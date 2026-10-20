import copy
import unittest

from scripts import review_ecd_with_codex as review


class FullReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [(1, '["有漏发","补发还是退款","补发"]'), (2, '["你好"]')]
        self.answer = {"records": [
            {"id": 1, "goal": "补寄服务", "status": "clear", "source_evidence": [1, 2]},
            {"id": 2, "goal": "问候", "status": "non_business", "source_evidence": [0]},
        ], "boundary_notes": "补发是选择后的业务目标，不是独立的确认意图。"}

    def test_requires_every_id_once(self):
        review.validate_answer(self.answer, self.rows)
        for records in [self.answer["records"][:1], self.answer["records"] * 2,
                        [self.answer["records"][0], self.answer["records"][0]]]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                review.validate_answer({**self.answer, "records": records}, self.rows)

    def test_rejects_missing_target_and_future_evidence(self):
        for indices in [[], [0], [3], [-1, 2], [True, 2]]:
            answer = copy.deepcopy(self.answer)
            answer["records"][0]["source_evidence"] = indices
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                review.validate_answer(answer, self.rows)

    def test_rejects_invalid_semantic_metadata(self):
        for key, value in [("goal", ""), ("goal", None), ("status", "done"), ("id", True)]:
            answer = copy.deepcopy(self.answer)
            answer["records"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                review.validate_answer(answer, self.rows)


if __name__ == "__main__":
    unittest.main()
