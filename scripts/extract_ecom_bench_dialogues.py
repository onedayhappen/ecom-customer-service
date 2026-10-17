"""Extract existing published ECom-Bench dialogues; no dialogue generation."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.data_plan.taxonomy import TAXONOMY_VERSION, VALID_INTENTS

COMMIT = "bc5018daaf45eea12330941bce68cebd293cfa85"
PAPER = "https://arxiv.org/html/2507.05639v2"
ARCHIVE = f"https://codeload.github.com/XiaoduoAILab/ECom-Bench/zip/{COMMIT}"
CACHE = ROOT / "data/source_cache/ecom_bench"
OUTPUT = ROOT / "data/fine_tuning/ecom_bench_extracted"
GROUP = "ecom-bench-paper-v2-appendix-A-price-protection-installation-invoice"

# Assigned individually by the assistant using only dialogue up to that user turn.
# No source task objective, persona, future reply or expected tool call is evidence.
ANNOTATIONS = {
    ("A.2", 2): ("others", "当前要求预约安装；31 类未覆盖安装服务，不属于配送咨询或换货。"),
    ("A.2", 3): ("others", "继续查询这件已购商品的安装服务信息；不是一般退换货政策或商品参数。"),
    ("A.2", 4): ("order_inquiry", "当前明确要求查询订单信息并给出订单号。"),
    ("A.2", 5): ("invoice_request", "切换为请求开具个人发票。"),
    ("A.2", 6): ("invoice_request", "补充客服为开票索要的订单号、抬头和联系方式，延续开票意图。"),
    ("A.3", 2): ("price_protection_service", "承接客服提出的价保申请，要求退差价至京东 E 卡；不是退货退款。"),
    ("A.3", 3): ("others", "切换为周四预约安装；当前分类未覆盖安装服务。"),
    ("A.3", 4): ("invoice_request", "切换为办理个人发票。"),
    ("A.3", 5): ("invoice_request", "回应开票信息追问，要求从订单取联系方式；不是独立订单查询。"),
}

class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)

def extract(html):
    parser = TextParser()
    parser.feed(html)
    text = " ".join(" ".join(parser.parts).split())
    headings = ["A.2 Failed Multimodal Dialogue Example", "A.3 Successful Multimodal Dialogue Example"]
    start = text.rindex(headings[0])
    middle = text.index(headings[1], start)
    stop = text.index("Experimental support", middle)
    sections = [text[start + len(headings[0]):middle], text[middle + len(headings[1]):stop]]
    dialogues = []
    for section, content in zip(("A.2", "A.3"), sections):
        # Only repair the four verified word-wrap artifacts in the paper HTML.
        for broken, repaired in (("quali fied", "qualified"), ("installa tion", "installation"),
                                 ("screen shot", "screenshot"), ("num ber", "number")):
            content = content.replace(broken, repaired)
        chunks = re.split(r"\b(User|Assistant):\s*", content)
        messages = [{"role": chunks[i].lower(), "content": chunks[i + 1].strip()}
                    for i in range(1, len(chunks), 2)]
        if len(messages) != (13 if section == "A.2" else 12) or messages[-1] != {"role": "user", "content": "###STOP###"}:
            raise ValueError(f"Unexpected appendix layout in {section}; inspect before importing")
        dialogues.append({"dialogue_id": f"paper-v2-{section}", "section": section,
                          "source_group": GROUP,
                          "source_url": PAPER + ("#A1.SS2" if section == "A.2" else "#A1.SS3"),
                          "language": "en", "origin": "published_simulated_trajectory",
                          "messages": messages})
    return dialogues

def annotate(dialogues):
    rows, excluded, used = [], [], set()
    for dialogue in dialogues:
        history, user_turn = [], 0
        for index, message in enumerate(dialogue["messages"]):
            if message["content"] == "###STOP###":
                excluded.append({"dialogue_id": dialogue["dialogue_id"], "message_index": index,
                                 "reason": "simulator_control_marker_not_user_language"})
                continue
            # Preserve opening assistant greeting in source, omit from user-first classifier input.
            if not history and message["role"] == "assistant":
                continue
            history.append(dict(message))
            if message["role"] != "user":
                continue
            user_turn += 1
            if user_turn == 1:
                excluded.append({"dialogue_id": dialogue["dialogue_id"], "message_index": index,
                                 "reason": "first_user_turn_has_no_prior_user_context"})
                continue
            key = (dialogue["section"], user_turn)
            label, reason = ANNOTATIONS[key]
            used.add(key)
            if label not in VALID_INTENTS:
                raise ValueError(label)
            rows.append({"id": f"{dialogue['dialogue_id']}-u{user_turn}",
                         "taxonomy_version": TAXONOMY_VERSION,
                         "dataset_version": "ecom-bench-published-extract-v1",
                         "split": "unassigned", "source_group": GROUP,
                         "source_dialogue_id": dialogue["dialogue_id"],
                         "source_url": dialogue["source_url"], "source_user_turn": user_turn,
                         "source_message_index": index, "language": "en",
                         "messages": [dict(m) for m in history], "expected_intent": label,
                         "annotation": {"method": "assistant_manual_per_turn",
                                        "independently_reviewed": False, "rationale": reason}})
    if used != set(ANNOTATIONS):
        raise ValueError("Annotation keys do not match the extracted source")
    return rows, excluded

def build(paper_path, archive_path):
    paper_bytes = paper_path.read_bytes()
    dialogues = extract(paper_bytes.decode("utf-8"))
    rows, excluded = annotate(dialogues)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.comment.decode() != COMMIT:
            raise ValueError("Upstream archive revision differs from reviewed commit")
        names = sorted(n for n in archive.namelist() if not n.endswith("/"))
        prefix = names[0].split("/")[0] + "/"
        paths = [name.removeprefix(prefix) for name in names]
        ignore = archive.read(prefix + ".gitignore").decode()
    counts = dict(sorted(Counter(r["expected_intent"] for r in rows).items()))
    report = {
        "taxonomy_version": TAXONOMY_VERSION,
        "source_repository": "https://github.com/XiaoduoAILab/ECom-Bench",
        "repository_commit": COMMIT, "archive_url": ARCHIVE,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "repository_file_inventory": paths, "upstream_gitignore": ignore,
        "repository_dialogue_logs_found": 0,
        "paper_url": PAPER, "paper_html_sha256": hashlib.sha256(paper_bytes).hexdigest(),
        "paper_license": "CC-BY-4.0",
        "paper_license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Wang et al., ECom-Bench: Can LLM Agent Resolve Real-World E-commerce Customer Support Issues?, arXiv:2507.05639v2, 2025",
        "source_dialogues": len(dialogues),
        "source_messages_including_controls": sum(len(d["messages"]) for d in dialogues),
        "natural_user_turns": sum(m["role"] == "user" and m["content"] != "###STOP###"
                                  for d in dialogues for m in d["messages"]),
        "accepted_multiturn_prefixes": len(rows), "class_counts": counts,
        "missing_labels": sorted(VALID_INTENTS - counts.keys()),
        "excluded_user_turns": excluded, "independent_task_groups": 1,
        "train_ready": False, "splits_created": [],
        "limitations": [
            "Only two published English simulated trajectories of the same task are available.",
            "No train/dev/test split: same-task prefixes must not cross partitions.",
            "No dialogue generation, template expansion, translation, OCR, or seed reuse.",
            "Only four HTML word-wrap artifacts and whitespace are normalized.",
            "Source assistant replies can contain errors; they are context, not execution targets.",
            "Annotation reasons and provenance must not enter model inputs.",
            "Image URLs are preserved as text; images were not fetched or interpreted."]}
    dump = lambda value: json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    return {"source_dialogues.json": dump(dialogues),
            "annotated_pool.jsonl": "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            "source_audit.json": dump(report)}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download pinned public sources into ignored cache")
    parser.add_argument("--check", action="store_true", help="Compare output without overwriting it")
    args = parser.parse_args()
    paper_path, archive_path = CACHE / "paper-v2.html", CACHE / "upstream-main.zip"
    if args.download:
        CACHE.mkdir(parents=True, exist_ok=True)
        for url, path in ((PAPER, paper_path), (ARCHIVE, archive_path)):
            with urlopen(url, timeout=60) as response:
                path.write_bytes(response.read())
    artifacts = build(paper_path, archive_path)
    if args.check:
        for name, content in artifacts.items():
            if (OUTPUT / name).read_text(encoding="utf-8") != content:
                raise SystemExit(f"Extraction differs: {name}")
    else:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for name, content in artifacts.items():
            (OUTPUT / name).write_text(content, encoding="utf-8")
    print("Verified 2 published dialogues -> 9 annotated prefixes, 4/31 labels, 1 task group; NOT train-ready.")

if __name__ == "__main__":
    main()
