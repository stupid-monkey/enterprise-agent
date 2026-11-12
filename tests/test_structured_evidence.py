import unittest

from eval.structured_evidence import structured_evidence_check


class StructuredEvidenceTests(unittest.TestCase):
    def test_comparison_requires_the_asked_attribute(self):
        question = "上海和北京的停车费标准一样吗？"
        self.assertEqual(structured_evidence_check(question,
            "上海和北京的会议室预定规则相同。"), "missing_attribute")
        self.assertEqual(structured_evidence_check(question,
            "上海和北京的停车费标准相同。"), "shape_match")
        self.assertEqual(structured_evidence_check(question,
            "上海停车费十元。北京会议室规则相同。"), "missing_comparison_entities")

    def test_numeric_unit_period_and_bound_must_match_same_sentence(self):
        self.assertEqual(structured_evidence_check("高铁每月餐补最多报多少钱？",
            "高铁每次餐补八十元。"), "missing_period")
        self.assertEqual(structured_evidence_check("高铁每月餐补最多报多少钱？",
            "高铁每月餐补八十元。"), "missing_bound")
        self.assertEqual(structured_evidence_check("高铁每月餐补最多报多少钱？",
            "高铁每月餐补上限八十元。"), "shape_match")
        self.assertEqual(structured_evidence_check("合规考试多少分算通过？",
            "合规考试每月三次，成绩另行通知。"), "missing_unit")
        self.assertEqual(structured_evidence_check("合规考试多少分算通过？",
            "合规考试六十分算通过。"), "shape_match")

    def test_unchecked_remains_explicit(self):
        self.assertEqual(structured_evidence_check("由谁召集采购评审？",
            "采购经理召集评审。"), "unchecked")
        self.assertEqual(structured_evidence_check("由谁召集采购评审？", None), "no_quote")


if __name__ == "__main__":
    unittest.main()
