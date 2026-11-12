import copy
import unittest

from eval.simulate_verifier_failures import simulate


def row(case, status, expected, checks):
    return {"case": case, "status": status, "expected_answerable": expected,
            "source_key": "doc", "claims": [{"text": "事实", "evidence_span": "原文"}]
            if status == "answered" else [], "verifier_checks": checks,
            "generation_ms": 100.0, "elapsed_ms": 150.0}


class SimulateVerifierFailuresTests(unittest.TestCase):
    def test_single_and_total_outage_do_not_change_original_report(self):
        report = {"dataset": "fake", "cases_selected": 3, "rows": [
            row("supported", "answered", True, [{"status": "supported"}]),
            row("already_rejected", "answered", False, [{"status": "insufficient"}]),
            row("refused", "refused", False, []),
        ]}
        original = copy.deepcopy(report)
        result = simulate(report)
        self.assertEqual(report, original)
        self.assertEqual(result["eligible_verified_answers"], 1)
        self.assertEqual(result["baseline_direct_outputs"], 2)
        self.assertEqual(result["baseline_verified_outputs"], 1)
        self.assertEqual(result["single_failure_scenarios"][0]["verified_outputs"], 0)
        self.assertEqual(result["all_verifiers_fail"]["direct_outputs"], 2)
        self.assertEqual(result["all_verifiers_fail"]["verified_outputs"], 0)
        self.assertEqual(result["all_verifiers_fail"]["withheld_by_verifier"],
                         ["supported", "already_rejected"])

    def test_no_successful_verification_has_no_injected_case(self):
        report = {"dataset": "fake", "cases_selected": 1,
                  "rows": [row("refused", "refused", False, [])]}
        result = simulate(report)
        self.assertEqual(result["single_failure_scenarios"], [])
        self.assertEqual(result["all_verifiers_fail"]["verified_outputs"], 0)


if __name__ == "__main__":
    unittest.main()
