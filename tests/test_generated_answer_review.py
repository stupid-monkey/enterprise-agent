import unittest

from eval.evaluate_generated_answers import review_cards
from eval.score_generated_answer_review import score


class GeneratedAnswerReviewTests(unittest.TestCase):
    def setUp(self):
        self.report = {"rows": [
            {"case": "yes", "question": "问题", "status": "answered", "answer": "回答",
             "claims": [{"text": "回答", "evidence_span": "证据"}], "source_title": "资料",
             "source_key": "doc"},
            {"case": "no", "question": "问题二", "status": "refused", "answer": "",
             "claims": [], "source_title": "资料", "source_key": "doc"},
        ]}
        self.cards = review_cards(self.report, {"doc": {"text": "证据"}})

    def test_pending_labels_are_not_success(self):
        result = score(self.report, self.cards)
        self.assertEqual((result["reviewed"], result["answered_reviewed"],
                          result["refusals_reviewed"]), (0, 0, 0))
        self.assertEqual(set(result["pending"]), {"yes", "no"})

    def test_completed_review_and_tampering(self):
        self.cards[0]["human_answer_supported"] = True
        self.cards[0]["human_citation_supported"] = False
        self.cards[1]["human_refusal_appropriate"] = True
        result = score(self.report, self.cards)
        self.assertEqual((result["reviewed"], result["answer_supported"],
                          result["citation_supported"], result["refusal_appropriate"]),
                         (2, 1, 0, 1))
        self.cards[0]["answer"] = "被修改"
        with self.assertRaisesRegex(ValueError, "REVIEW_CONTENT_CHANGED"):
            score(self.report, self.cards)
        self.cards[0]["answer"] = "回答"
        self.cards[0]["source_text"] = "被修改的证据"
        with self.assertRaisesRegex(ValueError, "REVIEW_SOURCE_CHANGED"):
            score(self.report, self.cards, {"doc": {"text": "证据"}})


if __name__ == "__main__":
    unittest.main()
