"""Prepare official Taobao ECD conversation windows for last-user intent annotation.

Preserve source text, discard negative responses and single-turn contexts, and
keep one unlabeled target per window. This is not an intent-labelled SFT export.
Only Python's standard library is required.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlencode
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/source_cache/ecd"
OUTPUT = ROOT / "data/fine_tuning/ecd_extracted"
ARCHIVE = CACHE / "E-commerce dataset.zip"
SOURCE_REPOSITORY = "https://github.com/cooelf/DeepUtteranceAggregation"
SOURCE_PAPER = "https://aclanthology.org/C18-1317/"
DOWNLOAD_URL = "https://drive.google.com/uc?export=download&id=154J-neBo20ABtSmJDvm7DK0eTuieAuvw"
# Prefer held-out data when an input appears in multiple original splits.
SPLITS = ("test", "dev", "train")
EXPECTED_LABEL_COUNTS = {
    "train": {"1": 500000, "0": 500000},
    "dev": {"1": 5000, "0": 5000},
    "test": {"1": 1000, "0": 9000},
}


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def context_key(context):
    # Normalize only for comparison, never rewrite the stored source utterances.
    return hashlib.sha256(dump([re.sub(r"\s+", "", x) for x in context]).encode()).hexdigest()


def prefix_keys(context):
    return [context_key(context[:end]) for end in range(3, len(context) + 1, 2)]


def parse_line(line):
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 3 or parts[0] not in {"0", "1"}:
        raise ValueError("Expected binary match label, context and candidate response")
    label, context, response = parts[0], parts[1:-1], parts[-1]
    if any(not value.strip() for value in context + [response]):
        raise ValueError("Empty context utterance or response")
    # Official data uses odd-length contexts. Roles are still an assumption,
    # not explicit annotations: user/assistant alternating, ending in user.
    if len(context) % 2 != 1:
        raise ValueError("Even-length context: cannot safely apply the role convention")
    return label, context, response


class DownloadForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs.get("action")
        if tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")


def download():
    CACHE.mkdir(parents=True, exist_ok=True)
    if ARCHIVE.is_file():
        if not zipfile.is_zipfile(ARCHIVE):
            raise ValueError("Existing archive is not a ZIP file")
        print("Using existing source archive", flush=True)
        return
    with ExitStack() as stack:
        response = stack.enter_context(urlopen(DOWNLOAD_URL, timeout=60))
        if "text/html" in response.headers.get("Content-Type", ""):
            form = DownloadForm()
            form.feed(response.read().decode("utf-8"))
            if form.action != "https://drive.usercontent.google.com/download":
                raise ValueError("Unrecognized Google Drive confirmation page")
            response = stack.enter_context(urlopen(form.action + "?" + urlencode(form.fields), timeout=60))
        temporary = ARCHIVE.with_suffix(".part")
        with temporary.open("wb") as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
    if not zipfile.is_zipfile(temporary):
        raise ValueError("Download did not return a ZIP archive")
    temporary.replace(ARCHIVE)


class JsonlSink:
    """Hash deterministic output; --check recomputes without writing anything."""
    def __init__(self, path, check):
        self.path = path
        self.check = check
        self.hash = hashlib.sha256()
        self.count = 0
        self.stream = None if check else path.open("wb")

    def emit(self, row):
        data = (dump(row) + "\n").encode("utf-8")
        self.hash.update(data)
        self.count += 1
        if self.stream:
            self.stream.write(data)

    def close(self):
        if self.stream:
            self.stream.close()
        if self.check and digest_file(self.path) != self.hash.hexdigest():
            raise ValueError(f"Reproduction mismatch: {self.path.name}")
        return {"rows": self.count, "sha256": self.hash.hexdigest()}


def make_records(split, source_line, context, response, key):
    identity = f"ecd-{split}-{source_line:07d}"
    dialogue = {
        "dialogue_id": identity, "source_dataset": "Taobao ECD",
        "source_split": split, "source_archive_member": f"E-commerce dataset/{split}.txt",
        "source_line": source_line, "source_match_label": 1,
        "context_sha256": key, "source_session_id": None,
        "unit": "conversation_window", "label_status": "unlabeled",
        "role_status": "inferred_alternating_not_verified",
        "messages": [{"role": "user" if i % 2 == 0 else "assistant",
                      "content": text, "source_utterance_index": i}
                     for i, text in enumerate(context)],
        # For provenance only. Neither preview nor classifier input includes it.
        "reference_response_excluded_from_input": response,
    }
    candidate = {
        "sample_id": identity + "-last-user", "dialogue_id": identity,
        "source_split": split, "source_line": source_line,
        "target_message_index": len(context) - 1, "context_sha256": key,
        "label_status": "unlabeled", "role_review_required": True,
        "single_order_or_product_review_required": True,
    }
    return dialogue, candidate


def materialize_prefix(dialogue, candidate):
    index = candidate["target_message_index"]
    if (candidate["dialogue_id"] != dialogue["dialogue_id"]
            or index != len(dialogue["messages"]) - 1
            or candidate["context_sha256"] != dialogue["context_sha256"]
            or len(dialogue["messages"]) < 3
            or dialogue["messages"][index]["role"] != "user"):
        raise ValueError("Candidate does not identify this window's last user turn")
    return [{"role": m["role"], "content": m["content"]}
            for m in dialogue["messages"][:index + 1]]


def build(check=False):
    if not check:
        OUTPUT.mkdir(parents=True, exist_ok=True)
    outputs, statistics = {}, {}
    # These maps contain only kept higher-priority splits, not training data.
    protected_full, protected_prefix = {}, {}
    exclusions = JsonlSink(OUTPUT / "excluded_positive_windows.jsonl", check)
    with zipfile.ZipFile(ARCHIVE) as archive:
        for split in SPLITS:
            member = f"E-commerce dataset/{split}.txt"
            counters, lengths = Counter(), Counter()
            source_hash = hashlib.sha256()
            seen, kept_full, kept_prefix = {}, {}, {}
            windows = JsonlSink(OUTPUT / f"{split}_dialogues.jsonl", check)
            candidates = JsonlSink(OUTPUT / f"{split}_candidates.jsonl", check)
            preview = JsonlSink(OUTPUT / f"{split}_annotation_preview.jsonl", check)
            with archive.open(member) as source:
                for number, raw in enumerate(source, 1):
                    source_hash.update(raw)
                    try:
                        label, context, response = parse_line(raw.decode("utf-8"))
                    except ValueError as exc:
                        raise ValueError(f"{member}:{number}: {exc}") from exc
                    counters["source_rows"] += 1
                    counters["positive_rows" if label == "1" else "negative_rows"] += 1
                    if label == "0":
                        continue
                    lengths[len(context)] += 1
                    reason, duplicate_of = None, None
                    if len(context) < 3:
                        reason = "single_turn"
                        key = context_key(context)
                    else:
                        key = context_key(context)
                        if key in seen:
                            reason, duplicate_of = "duplicate_context_in_split", seen[key]
                        elif key in protected_full:
                            reason, duplicate_of = "duplicate_context_across_splits", protected_full[key]
                        else:
                            keys = prefix_keys(context)
                            # Detect either direction of exact prefix containment.
                            duplicate_of = protected_prefix.get(key)
                            if duplicate_of is None:
                                duplicate_of = next((protected_full[k] for k in keys if k in protected_full), None)
                            if duplicate_of is not None:
                                reason = "prefix_containment_across_splits"
                    identity = f"ecd-{split}-{number:07d}"
                    if len(context) >= 3:
                        seen.setdefault(key, identity)
                    if reason:
                        counters[reason] += 1
                        exclusions.emit({"dialogue_id": identity, "source_split": split,
                                         "source_line": number, "reason": reason,
                                         "related_dialogue_id": duplicate_of,
                                         "context_sha256": key})
                        continue
                    dialogue, candidate = make_records(split, number, context, response, key)
                    windows.emit(dialogue)
                    candidates.emit(candidate)
                    counters["kept_windows"] += 1
                    counters["kept_context_messages"] += len(context)
                    if preview.count < 20:
                        preview.emit({"sample_id": candidate["sample_id"],
                                      "label_status": "unlabeled", "role_review_required": True,
                                      "messages": materialize_prefix(dialogue, candidate)})
                    if split != "train":
                        kept_full[key] = identity
                        for prefix in keys:
                            kept_prefix.setdefault(prefix, identity)
            expected = EXPECTED_LABEL_COUNTS[split]
            if (counters["positive_rows"], counters["negative_rows"]) != (expected["1"], expected["0"]):
                raise ValueError(f"Unexpected source counts: {split}: {counters}")
            protected_full.update(kept_full)
            for key, identity in kept_prefix.items():
                protected_prefix.setdefault(key, identity)
            for sink in (windows, candidates, preview):
                outputs[sink.path.name] = sink.close()
            statistics[split] = {
                **dict(counters), "source_member": member,
                "source_bytes": archive.getinfo(member).file_size,
                "source_sha256": source_hash.hexdigest(),
                "positive_context_length_distribution": dict(sorted(lengths.items())),
            }
            print(f"{split}: {counters['kept_windows']} windows / {counters['positive_rows']} positive rows", flush=True)
    outputs[exclusions.path.name] = exclusions.close()
    report = {
        "extraction_version": "ecd-last-user-unlabeled-v1",
        "source_repository": SOURCE_REPOSITORY, "source_paper": SOURCE_PAPER,
        "source_download_url": DOWNLOAD_URL,
        "archive_sha256": digest_file(ARCHIVE), "archive_bytes": ARCHIVE.stat().st_size,
        "splits": statistics, "outputs": outputs,
        "total_kept_windows": sum(s["kept_windows"] for s in statistics.values()),
        "intent_labels_assigned": 0, "training_ready": False,
        "checks": {
            "original_text_preserved": True, "source_binary_label_counts_verified": True,
            "negative_responses_excluded": True, "single_turn_contexts_excluded": True,
            "unique_whitespace_normalized_contexts": True,
            "cross_split_exact_prefix_containment_removed": True,
            "retained_original_split": True, "target_is_last_user": True,
            "reference_response_excluded_from_preview": True,
        },
        "policies": {
            "dedup_priority": list(SPLITS), "within_split": "first source line wins",
            "comparison": "whitespace-normalized utterance sequences; source text unchanged",
            "target": "one final user target per retained conversation window",
            "roles": "inferred user/assistant alternation; not explicit source annotations",
        },
        "limitations": [
            "Windows are not necessarily independent or complete source sessions; no session IDs are provided.",
            "Exact context/prefix checks cannot exclude shared partial histories, suffix windows or semantic duplicates.",
            "Roles must be reviewed before training; odd context length does not prove correct speaker assignment.",
            "Current-turn intent and single-order/product scope are unlabelled; no class coverage is asserted.",
            "Reference response is future information and must not be shown to intent annotators or classifiers.",
            "Source 0/1 labels measure response matching, not intent or whether the user's request is valid.",
            "No separately verified data license was supplied in the archive; corpus remains local and Git-ignored.",
        ],
    }
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    path = OUTPUT / "extraction_report.json"
    if check:
        # JSON object keys in the length histograms become strings on disk.
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("Reproduction mismatch: extraction_report.json")
    else:
        path.write_text(content, encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--check", action="store_true", help="Recompute and verify all outputs without writing")
    args = parser.parse_args()
    if args.check and args.download:
        parser.error("--check cannot be combined with --download")
    if args.download:
        download()
    report = build(check=args.check)
    print(f"Total: {report['total_kept_windows']} unlabeled windows; training_ready=false")


if __name__ == "__main__":
    main()
