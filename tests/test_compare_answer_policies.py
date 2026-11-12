import unittest

from eval.compare_answer_policies import compare


def row(name, expected, status, checks):
    return {"case": name, "expected_answerable": expected, "status": status,
            "source_key": "doc", "claims": [{"text": "事实", "evidence_span": "来源"}]
            if status == "answered" else [], "verifier_checks": checks,
            "generation_ms": 100.0, "elapsed_ms": 150.0}


class CompareAnswerPoliciesTests(unittest.TestCase):
    def test_gate_withholds_rejected_error_and_missing_verification(self):
        rows = [row("good", True, "answered", [{"status": "supported"}]),
                row("bad", False, "answered", [{"status": "insufficient"}]),
                row("error", True, "answered", [{"status": "error"}]),
                row("unchecked", False, "answered", []),
                row("refused", False, "refused", [])]
        result = compare({"dataset": "fake", "cases_selected": 5, "rows": rows})
        self.assertEqual(result["raw"]["outputs"], 4)
        self.assertEqual(result["verified"]["outputs"], 1)
        self.assertEqual(result["verified"]["answerable_withheld"], 1)
        self.assertEqual(set(result["withheld_by_verifier"]), {"bad", "error", "unchecked"})

    def test_incomplete_report_fails(self):
        with self.assertRaisesRegex(ValueError, "REPORT_CASE_SET_INVALID"):
            compare({"dataset": "fake", "cases_selected": 2, "rows": [row("one", True, "refused", [])]})

    def test_no_verifier_run_cannot_be_counted_as_supported(self):
        result = compare({"dataset": "fake", "cases_selected": 1,
                          "rows": [row("only", True, "answered", [])]})
        self.assertEqual(result["raw"]["outputs"], 1)
        self.assertEqual(result["verified"]["outputs"], 0)
        self.assertEqual(result["rows"][0]["gate_reason"], "verifier_not_run")


if __name__ == "__main__":
    unittest.main()
