import copy
import unittest

from eval.compare_generated_answer_repeats import compare


def report():
    return {"dataset": "synthetic", "retrieval_report_sha256": "hash",
            "generator_model": "model", "verifier_model": "model", "cases_selected": 2,
            "generator_reported_usage": None, "verifier_reported_usage": None,
            "rows": [
                {"case": "yes", "status": "answered", "answer": "事实",
                 "source_key": "doc", "verifier_all_supported": True,
                 "verifier_checks": [{"status": "supported"}], "elapsed_ms": 100.0},
                {"case": "no", "status": "refused", "answer": "",
                 "source_key": "doc", "verifier_all_supported": None,
                 "verifier_checks": [], "elapsed_ms": 80.0},
            ]}


class CompareGeneratedAnswerRepeatsTests(unittest.TestCase):
    def test_identical_runs_are_stable(self):
        result = compare([report(), report()])
        self.assertEqual((result["status_stable_cases"], result["answer_stable_cases"],
                          result["verifier_comparable_cases"],
                          result["verifier_stable_answered_cases"]), (2, 2, 1, 1))
        self.assertEqual(result["candidate_request_latency_by_run"][0]["count"], 2)

    def test_verifier_error_and_changed_answer_are_reported(self):
        changed = copy.deepcopy(report())
        changed["rows"][0]["answer"] = "另一种说法"
        changed["rows"][0]["verifier_all_supported"] = False
        changed["rows"][0]["verifier_checks"] = [{"status": "error"}]
        result = compare([report(), changed])
        self.assertEqual(result["status_stable_cases"], 2)
        self.assertEqual(result["answer_stable_cases"], 1)
        self.assertEqual(result["verifier_comparable_cases"], 1)
        self.assertEqual(result["verifier_stable_answered_cases"], 0)
        self.assertEqual(result["verifier_error_answer_count"], 1)

    def test_mismatched_cases_or_source_is_rejected(self):
        changed = copy.deepcopy(report())
        changed["retrieval_report_sha256"] = "other"
        with self.assertRaisesRegex(ValueError, "REPEAT_INPUT_MISMATCH"):
            compare([report(), changed])
        changed = copy.deepcopy(report())
        changed["rows"][1]["case"] = "yes"
        with self.assertRaisesRegex(ValueError, "REPEAT_INPUT_MISMATCH"):
            compare([report(), changed])


if __name__ == "__main__":
    unittest.main()
