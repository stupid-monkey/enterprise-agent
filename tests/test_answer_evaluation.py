import unittest

from eval.evaluate_answers import evaluate


class AnswerEvaluationTests(unittest.TestCase):
    def test_fixed_answer_quality_gate(self):
        report = evaluate()
        self.assertEqual(report["dataset_version"], "m3-synthetic-v1")
        self.assertEqual(report["cases"], 9)
        self.assertEqual(report["passed"], report["cases"], report["results"])
        self.assertEqual(report["citation_supported"], report["citation_total"])
        self.assertEqual(report["refusals_passed"], report["refusal_total"])


if __name__ == "__main__":
    unittest.main()
