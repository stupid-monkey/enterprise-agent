"""The synthetic comparison must fail closed and report only measured facts."""

import unittest

from eval.evaluate_composition import SyntheticModel, evaluate, run_case


class CompositionEvaluationTests(unittest.TestCase):
    def test_three_variants_use_same_cases_and_report_call_counts(self):
        report = evaluate()
        self.assertEqual(report["dataset_version"], "m5.5-synthetic-v1")
        self.assertEqual(len(report["dataset_sha256"]), 64)
        self.assertFalse(report["real_model_execution"])
        self.assertFalse(report["real_model_quality_measured"])
        self.assertFalse(report["cost_measured"])
        self.assertFalse(report["performance_benchmark"])
        self.assertEqual({name: (row["passed"], row["cases"], row["model_calls"])
                          for name, row in report["summary"].items()},
                         {"single_agent": (7, 7, 4), "fixed_workflow": (7, 7, 12),
                          "collaboration": (7, 7, 12)})
        self.assertTrue(all(row["passed"] for row in report["results"]))

    def test_unapproved_source_and_fabricated_quote_fail(self):
        case = {"name": "bad_output", "role": "employee", "question": "差旅报销审批流程",
                "expected_ids": ["1"], "expected_fact": "部门负责人审批"}

        class BadSource(SyntheticModel):
            def complete(self, stage, payload):
                result = super().complete(stage, payload)
                if stage in ("single_agent", "compose_supervisor"):
                    result["source_ids"] = ["5"]
                return result

        class BadQuote(SyntheticModel):
            def complete(self, stage, payload):
                result = super().complete(stage, payload)
                if stage == "knowledge":
                    result["quotes"] = ["虚构原文"]
                return result

        self.assertFalse(run_case("single_agent", case, BadSource())["passed"])
        self.assertFalse(run_case("collaboration", case, BadSource())["passed"])
        self.assertFalse(run_case("fixed_workflow", case, BadQuote())["passed"])

    def test_revoked_source_never_reaches_model(self):
        case = {"name": "revoked", "role": "employee", "question": "差旅报销审批流程",
                "expected_ids": [], "deny_id": "1"}
        for variant in ("single_agent", "fixed_workflow", "collaboration"):
            with self.subTest(variant=variant):
                result = run_case(variant, case)
                self.assertTrue(result["passed"])
                self.assertEqual(result["status"], "no_evidence")
                self.assertEqual(result["model_calls"], 0)

    def test_usage_is_reported_only_when_endpoint_supplies_it(self):
        case = {"name": "usage", "role": "employee", "question": "差旅报销审批流程",
                "expected_ids": ["1"], "expected_fact": "部门负责人审批"}

        class MeteredModel(SyntheticModel):
            def complete(self, stage, payload):
                result = super().complete(stage, payload)
                self.last_usage = {"input_tokens": 100, "output_tokens": 20}
                return result

        result = run_case("single_agent", case, MeteredModel())
        self.assertTrue(result["passed"])
        self.assertEqual(result["usage_reported_calls"], 1)
        self.assertEqual(result["reported_input_tokens"], 100)
        self.assertEqual(result["reported_output_tokens"], 20)

    def test_review_text_is_opt_in_and_contains_generated_draft(self):
        case = {"name": "review", "role": "employee", "question": "差旅报销审批流程",
                "expected_ids": ["1"], "expected_fact": "部门负责人审批"}
        ordinary = run_case("single_agent", case)
        review = run_case("single_agent", case, include_review_text=True)
        self.assertNotIn("review_text", ordinary)
        self.assertIn("部门负责人审批", review["review_text"])

    def test_fact_check_ignores_whitespace_only(self):
        case = {"name": "spacing", "role": "employee", "question": "员工年假标准",
                "expected_ids": ["2"], "expected_fact": "5 天年假"}

        class CompactModel(SyntheticModel):
            def complete(self, stage, payload):
                result = super().complete(stage, payload)
                if stage == "single_agent":
                    result["draft"] = result["draft"].replace("5 天年假", "5天年假")
                return result

        self.assertTrue(run_case("single_agent", case, CompactModel())["expected_fact_present"])


if __name__ == "__main__":
    unittest.main()
