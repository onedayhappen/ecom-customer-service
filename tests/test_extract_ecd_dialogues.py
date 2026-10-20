"""Verify source fidelity, future-response isolation and split leakage filtering."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import extract_ecd_dialogues as ecd


class EcdExtractionTests(unittest.TestCase):
    def test_parser_rejects_ambiguous_structure(self):
        for source in ("2\t问\t答", "1\t问\t答\t问", "1\t\t答"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                ecd.parse_line(source)
        label, context, reply = ecd.parse_line("1\t原 文\t客服 原话\t最后 问题\t后续 回复\r\n")
        self.assertEqual((label, context, reply), ("1", ["原 文", "客服 原话", "最后 问题"], "后续 回复"))

    def test_preview_excludes_future_and_metadata(self):
        context = ["想 退货", "为什么 退货", "尺寸 不合适"]
        dialogue, candidate = ecd.make_records("train", 1, context, "已经退款", ecd.context_key(context))
        messages = ecd.materialize_prefix(dialogue, candidate)
        self.assertEqual([m["content"] for m in messages], context)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertTrue(all(set(m) == {"role", "content"} for m in messages))
        self.assertNotIn("已经退款", json.dumps(messages, ensure_ascii=False))
        with self.assertRaises(ValueError):
            ecd.materialize_prefix(dialogue, {**candidate, "target_message_index": 0})

    def test_full_pipeline_dedup_and_read_only_reproduction(self):
        sources = {
            "test": ["1\t甲\t乙\t丙\t答一", "1\t丁\t戊\t己\t庚\t辛\t答二"],
            "dev": ["1\t甲\t乙\t丙\t不同回复", "1\t丁\t戊\t己\t答三",
                    "1\t甲\t乙\t丙\t新增客服\t新增用户\t答四", "1\t壬\t癸\t子\t答五"],
            "train": ["1\t单轮\t答", "0\t错\t误\t回\t负回复",
                      "1\t买 耳机\t预算多少\t三 百元\t未来回复",
                      "1\t买耳机\t预算多少\t三百元\t另一回复",
                      "1\t壬\t癸\t子\t答六"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, output = root / "source.zip", root / "out"
            with zipfile.ZipFile(archive, "w") as z:
                for split, lines in sources.items():
                    z.writestr(f"E-commerce dataset/{split}.txt", "\n".join(lines) + "\n")
            expected = {s: {"1": sum(x.startswith("1") for x in rows),
                            "0": sum(x.startswith("0") for x in rows)}
                        for s, rows in sources.items()}
            with patch.multiple(ecd, ARCHIVE=archive, OUTPUT=output, EXPECTED_LABEL_COUNTS=expected), contextlib.redirect_stdout(io.StringIO()):
                report = ecd.build()
                self.assertEqual(report["total_kept_windows"], 4)
                self.assertEqual(report["splits"]["dev"]["prefix_containment_across_splits"], 2)
                self.assertEqual(report["splits"]["train"]["duplicate_context_in_split"], 1)
                self.assertEqual(report["splits"]["train"]["duplicate_context_across_splits"], 1)
                row = json.loads((output / "train_dialogues.jsonl").read_text(encoding="utf-8"))
                self.assertEqual(row["source_line"], 3)
                self.assertEqual(row["messages"][0]["content"], "买 耳机")
                self.assertEqual(row["reference_response_excluded_from_input"], "未来回复")
                snapshots = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
                ecd.build(check=True)
                self.assertEqual(snapshots, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()})
                with (output / "train_candidates.jsonl").open("a", encoding="utf-8") as f:
                    f.write("{}\n")
                with self.assertRaisesRegex(ValueError, "Reproduction mismatch"):
                    ecd.build(check=True)


if __name__ == "__main__":
    unittest.main()
