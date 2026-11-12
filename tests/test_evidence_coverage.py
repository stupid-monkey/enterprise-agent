import unittest

from eval.evidence_coverage import evidence_coverage


class EvidenceCoverageTests(unittest.TestCase):
    def test_comparison_needs_both_entities(self):
        question = "广州和深圳的餐补标准相同吗？"
        self.assertEqual(evidence_coverage(question, "广州餐补是每天二十元。"),
                         "missing_comparison_entities")
        self.assertEqual(evidence_coverage(question, "广州每天二十元，深圳每天三十元。"),
                         "surface_covered")

    def test_quantity_needs_a_quantity_in_the_quote(self):
        question = "设备每月最多可借几次？"
        self.assertEqual(evidence_coverage(question, "设备借用需要登记。"), "missing_quantity")
        self.assertEqual(evidence_coverage(question, "设备每月最多借三次。"), "surface_covered")
        self.assertEqual(evidence_coverage("企业卡每月额度多少？", "月末三天内上传凭证。"),
                         "missing_quantity")
        self.assertEqual(evidence_coverage("企业卡每月额度多少？", "企业卡月额度为三万元。"),
                         "surface_covered")

    def test_unchecked_is_not_an_answerability_claim(self):
        self.assertEqual(evidence_coverage("由谁审批采购？", "采购经理审批。"), "unchecked")
        self.assertEqual(evidence_coverage("由谁审批采购？", None), "no_quote")


if __name__ == "__main__":
    unittest.main()
