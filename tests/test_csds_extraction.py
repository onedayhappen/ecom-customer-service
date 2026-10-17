"""Integrity checks for source extraction and future-message exclusion."""
import unittest

from scripts.extract_csds_dialogues import extract_dialogue, prefix_indices, materialize_prefix, verify_groups


def source():
    return {"DialogueID": 1, "Session_id": "source-session", "QRole": "用户",
            "Dialogue": [{"speaker": role, "turn": i, "utterance": text}
                         for i, (role, text) in enumerate([
                             ("Q", "原 用户 消息"), ("Q", "连续 消息"),
                             ("A", "原 客服 回复"), ("Q", "当前 用户 消息"),
                             ("A", "不能泄漏的未来回复")])],
            "FinalSumm": "不能进入输入的摘要", "QA": [{"Topic": "不能照搬的原标签"}]}


class CSDSExtractionTests(unittest.TestCase):
    def test_source_text_and_consecutive_roles_are_preserved(self):
        row = source()
        dialogue = extract_dialogue(row, "train")
        self.assertEqual([m["content"] for m in dialogue["messages"]],
                         [m["utterance"] for m in row["Dialogue"]])
        self.assertEqual([m["role"] for m in dialogue["messages"][:2]], ["user", "user"])
        self.assertNotIn("FinalSumm", dialogue)
        self.assertNotIn("QA", dialogue)

    def test_prefix_requires_both_prior_user_and_agent_and_excludes_future(self):
        dialogue = extract_dialogue(source(), "train")
        candidates = list(prefix_indices(dialogue))
        self.assertEqual([c["target_source_turn"] for c in candidates], [3])
        messages = materialize_prefix(dialogue, candidates[0])
        self.assertEqual(messages[-1], {"role": "user", "content": "当前 用户 消息"})
        self.assertEqual(len(messages), 4)
        self.assertTrue(all(set(m) == {"role", "content"} for m in messages))
        self.assertNotIn("不能泄漏", str(messages))

    def test_cannot_materialize_mismatched_candidate(self):
        dialogue = extract_dialogue(source(), "train")
        candidate = next(prefix_indices(dialogue))
        for override in ({"dialogue_id": "other"}, {"target_message_index": 4},
                         {"target_source_turn": 0}):
            with self.assertRaises(ValueError):
                materialize_prefix(dialogue, dict(candidate, **override))

    def test_nonbuyer_is_excluded(self):
        self.assertIsNone(extract_dialogue(dict(source(), QRole="商家"), "train"))

    def test_cross_split_source_session_is_rejected(self):
        first = extract_dialogue(source(), "train")
        second = extract_dialogue(source(), "test")
        with self.assertRaisesRegex(ValueError, "Repeated original session"):
            verify_groups([first, second])

    def test_cross_split_text_duplicate_is_rejected_despite_different_ids(self):
        first = extract_dialogue(source(), "train")
        row = source()
        row["DialogueID"], row["Session_id"] = 2, "different-source-session"
        for m in row["Dialogue"]:
            m["utterance"] = m["utterance"].replace(" ", "")
        with self.assertRaisesRegex(ValueError, "Duplicate dialogue text"):
            verify_groups([first, extract_dialogue(row, "test")])


if __name__ == "__main__":
    unittest.main()
