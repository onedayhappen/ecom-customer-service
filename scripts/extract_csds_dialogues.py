"""Extract existing CSDS buyer dialogues and unlabeled multi-turn prefix indices.

No summaries, original topic labels, generated replies, or new intent labels are
included. Source files and large extracted datasets remain local Git-ignored data.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/source_cache/csds"
OUTPUT = ROOT / "data/fine_tuning/csds_extracted"
SPLITS = ("train", "val", "test")
SOURCE_IDS = {
    "train": "1-xwmTxDAZXlt3YhPBgQWETzw_yM5Xui2",
    "val": "1JLW5iRUjdFz1BUGypyGHAm6vEkMI20sS",
    "test": "1xpHJWJd5kLnq9tKqzqE-928FgYEryka1",
}
SOURCE_COUNTS = {"train": 9101, "val": 800, "test": 800}
SOURCE_REPOSITORY = "https://github.com/xiaolinAndy/CSDS"
SOURCE_PAPER = "https://aclanthology.org/2021.emnlp-main.365/"

def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def extract_dialogue(row, split):
    if row["QRole"] != "用户":
        return None
    dialogue_id = row["DialogueID"]
    session_id = row["Session_id"]
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Missing original session ID")
    messages = []
    for index, source in enumerate(row["Dialogue"]):
        if source["speaker"] not in {"Q", "A"}:
            raise ValueError(f"Unknown speaker: {dialogue_id}:{index}")
        if source["turn"] != index:
            raise ValueError(f"Non-contiguous source turns: {dialogue_id}")
        text = source["utterance"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Empty/nontext utterance: {dialogue_id}:{index}")
        messages.append({"role": "user" if source["speaker"] == "Q" else "assistant",
                         "content": text, "source_turn": source["turn"]})
    if sum(m["role"] == "user" for m in messages) < 2 or not any(m["role"] == "assistant" for m in messages):
        return None
    return {"dialogue_id": f"csds-{split}-{dialogue_id}",
            "source_dataset": "CSDS", "source_split": split,
            "source_dialogue_id": dialogue_id, "source_session_id": session_id,
            "source_group": f"jddc-session:{session_id}", "q_role": row["QRole"],
            "language": "zh", "label_status": "unlabeled", "messages": messages}

def prefix_indices(dialogue):
    users, assistants = 0, 0
    for index, message in enumerate(dialogue["messages"]):
        if message["role"] == "assistant":
            assistants += 1
            continue
        users += 1
        # A genuine multi-turn classification input needs prior user AND agent text.
        if users < 2 or assistants < 1:
            continue
        yield {"sample_id": f"{dialogue['dialogue_id']}-t{message['source_turn']}",
               "dialogue_id": dialogue["dialogue_id"],
               "source_session_id": dialogue["source_session_id"],
               "source_group": dialogue["source_group"], "source_split": dialogue["source_split"],
               "target_message_index": index, "target_source_turn": message["source_turn"],
               "context_user_messages": users, "context_assistant_messages": assistants,
               "label_status": "unlabeled"}

def materialize_prefix(dialogue, candidate):
    if candidate["dialogue_id"] != dialogue["dialogue_id"]:
        raise ValueError("Candidate belongs to a different dialogue")
    index = candidate["target_message_index"]
    if not isinstance(index, int) or not 0 <= index < len(dialogue["messages"]):
        raise ValueError("Invalid target index")
    eligible = {c["target_message_index"]: c for c in prefix_indices(dialogue)}
    if candidate != eligible.get(index):
        raise ValueError("Candidate does not match a valid source prefix")
    return [{"role": m["role"], "content": m["content"]}
            for m in dialogue["messages"][:index + 1]]

def verify_groups(dialogues):
    sessions, fingerprints, ids = {}, {}, set()
    for dialogue in dialogues:
        identity = dialogue["dialogue_id"]
        if identity in ids:
            raise ValueError(f"Duplicate dialogue ID: {identity}")
        ids.add(identity)
        session = dialogue["source_session_id"]
        if session in sessions:
            raise ValueError(f"Repeated original session: {sessions[session]}, {identity}")
        sessions[session] = identity
        # Whitespace-insensitive whole-dialogue deduplication, never rewrite text.
        key = sha(dump([(m["role"], re.sub(r"\s+", "", m["content"]))
                        for m in dialogue["messages"]]).encode("utf-8"))
        if key in fingerprints:
            raise ValueError(f"Duplicate dialogue text: {fingerprints[key]}, {identity}")
        fingerprints[key] = identity

def download():
    CACHE.mkdir(parents=True, exist_ok=True)
    for split in SPLITS:
        url = "https://drive.google.com/uc?export=download&id=" + SOURCE_IDS[split]
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as response:
            data = response.read()
        rows = json.loads(data.decode("utf-8-sig"))
        if not isinstance(rows, list) or len(rows) != SOURCE_COUNTS[split]:
            raise ValueError(f"Unexpected source payload: {split}")
        (CACHE / f"{split}.json").write_bytes(data)

def build(check=False):
    extracted, report_splits, exclusions = {}, {}, []
    for split in SPLITS:
        path = CACHE / f"{split}.json"
        data = path.read_bytes()
        rows = json.loads(data.decode("utf-8-sig"))
        if not isinstance(rows, list) or len(rows) != SOURCE_COUNTS[split]:
            raise ValueError(f"Unexpected source count: {split}")
        dialogues = []
        for row in rows:
            dialogue = extract_dialogue(row, split)
            if dialogue is None:
                exclusions.append({"source_split": split, "source_dialogue_id": row["DialogueID"],
                                   "source_session_id": row["Session_id"], "q_role": row["QRole"],
                                   "reason": "not_buyer" if row["QRole"] != "用户" else "not_multiturn"})
            else:
                dialogues.append(dialogue)
        extracted[split] = dialogues
        report_splits[split] = {
            "source_file": f"data/source_cache/csds/{split}.json",
            "download_url": "https://drive.google.com/uc?export=download&id=" + SOURCE_IDS[split],
            "source_sha256": sha(data), "source_bytes": len(data), "source_dialogues": len(rows),
            "source_role_counts": dict(Counter(row["QRole"] for row in rows)),
            "kept_dialogues": len(dialogues),
            "messages": sum(len(d["messages"]) for d in dialogues),
            "user_messages": sum(m["role"] == "user" for d in dialogues for m in d["messages"]),
            "eligible_user_prefixes": sum(1 for d in dialogues for _ in prefix_indices(d)),
        }
    # Fail before writing any outputs on overlap or source anomalies.
    verify_groups([d for split in SPLITS for d in extracted[split]])
    if not check:
        OUTPUT.mkdir(parents=True, exist_ok=True)
    output_hashes = {}
    def emit(name, records):
        content = "".join(dump(record) + "\n" for record in records)
        output_hashes[name] = sha(content.encode("utf-8"))
        path = OUTPUT / name
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise ValueError(f"Reproduction mismatch: {name}")
        else:
            path.write_text(content, encoding="utf-8")
    for split in SPLITS:
        emit(f"{split}_dialogues.jsonl", extracted[split])
        emit(f"{split}_candidates.jsonl", (c for d in extracted[split] for c in prefix_indices(d)))
    emit("excluded_dialogues.jsonl", exclusions)
    report = {
        "extraction_version": "csds-buyer-multiturn-v1",
        "source_repository": SOURCE_REPOSITORY, "source_paper": SOURCE_PAPER,
        "source_download_listing": "https://drive.google.com/drive/folders/1IrpEUTR2ZanN0ZKmKcbaXjruTAD9JLkF",
        "splits": report_splits, "excluded_dialogues": len(exclusions),
        "total_kept_dialogues": sum(s["kept_dialogues"] for s in report_splits.values()),
        "total_eligible_user_prefixes": sum(s["eligible_user_prefixes"] for s in report_splits.values()),
        "output_sha256": output_hashes, "intent_labels_assigned": 0, "training_ready": False,
        "checks": {"unique_source_sessions": True, "unique_whitespace_normalized_dialogues": True,
                   "original_split_preserved": True, "source_turns_contiguous": True,
                   "nonempty_text": True, "summaries_and_topic_labels_excluded": True},
        "limitations": [
            "Intent and single-order/entity scope still require per-prefix annotation review.",
            "Original segmented whitespace and consecutive same-speaker messages are preserved.",
            "A prefix may begin with an agent message; this is source-faithful, not a final SFT export.",
            "Exact duplicate checks do not establish absence of semantic near-duplicates.",
            "Source data has no separately verified dataset license; cached/extracted corpus stays local.",
            "Existing source annotations and future replies must not determine current-user labels.",
            "Candidate indices reference source messages; they contain no fabricated intent labels.",
            "User prefixes are correlated views of dialogues, not independent conversations."]}
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    path = OUTPUT / "extraction_report.json"
    if check:
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("Reproduction mismatch: extraction_report.json")
    else:
        path.write_text(text, encoding="utf-8")
    return report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--preview", help="Existing dialogue ID to materialize for annotation")
    parser.add_argument("--turn", type=int, help="Original user message turn index (zero-based)")
    args = parser.parse_args()
    if args.preview:
        if args.turn is None or args.download or args.check:
            parser.error("--preview requires --turn and cannot accompany --download/--check")
        for split in SPLITS:
            for line in (OUTPUT / f"{split}_dialogues.jsonl").read_text(encoding="utf-8").splitlines():
                dialogue = json.loads(line)
                if dialogue["dialogue_id"] != args.preview:
                    continue
                candidate = next((c for c in prefix_indices(dialogue) if c["target_source_turn"] == args.turn), None)
                if candidate is None:
                    parser.error("Turn is not an eligible multi-turn user target")
                print(json.dumps({"sample_id": candidate["sample_id"], "messages": materialize_prefix(dialogue, candidate)},
                                 ensure_ascii=False, indent=2))
                return
        parser.error("Dialogue not found")
    if args.turn is not None:
        parser.error("--turn requires --preview")
    if args.download:
        download()
    report = build(check=args.check)
    print(json.dumps({split: report["splits"][split] for split in SPLITS}, ensure_ascii=False, indent=2))
    print(f"Kept {report['total_kept_dialogues']} dialogues; {report['total_eligible_user_prefixes']} unlabeled prefixes. Training ready: false.")

if __name__ == "__main__":
    main()
