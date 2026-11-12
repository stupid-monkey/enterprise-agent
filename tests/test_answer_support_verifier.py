import unittest

from eval.answer_support_verifier import VerificationInvalid, verify_answer


class FakeVerifier:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def complete(self, question, answer, quote):
        self.calls += 1
        return self.result


class AnswerSupportVerifierTests(unittest.TestCase):
    def test_supported_requires_exact_source_span(self):
        source = "上海每晚六百元。北京每晚五百元。"
        model = FakeVerifier({"verdict": "supported", "evidence_span": source,
                              "reason_code": "direct"})
        self.assertEqual(verify_answer(model, "两地一样吗？", "不一样。", source).verdict,
                         "supported")
        model.result["evidence_span"] = "虚构的原文"
        with self.assertRaisesRegex(VerificationInvalid, "MODEL_UNSUPPORTED_SPAN"):
            verify_answer(model, "两地一样吗？", "不一样。", source)

    def test_no_quote_skips_model_and_bad_schema_fails_closed(self):
        model = FakeVerifier({"verdict": "supported", "evidence_span": "x",
                              "reason_code": "direct"})
        self.assertIsNone(verify_answer(model, "问题", "答案", None))
        self.assertEqual(model.calls, 0)
        model.result = {"verdict": "supported", "evidence_span": "", "reason_code": "direct"}
        with self.assertRaisesRegex(VerificationInvalid, "MODEL_OUTPUT_INVALID"):
            verify_answer(model, "问题", "答案", "来源")
        model.result = {"verdict": "insufficient", "evidence_span": "", "reason_code": "direct"}
        with self.assertRaisesRegex(VerificationInvalid, "MODEL_OUTPUT_INVALID"):
            verify_answer(model, "问题", "答案", "来源")
        model.result = {"verdict": "supported", "evidence_span": "来源",
                        "reason_code": "direct", "unexpected": True}
        with self.assertRaisesRegex(VerificationInvalid, "MODEL_OUTPUT_INVALID"):
            verify_answer(model, "问题", "答案", "来源")

    def test_insufficient_can_cite_related_source(self):
        model = FakeVerifier({"verdict": "insufficient", "evidence_span": "每次八十元",
                              "reason_code": "wrong_scope"})
        decision = verify_answer(model, "每月上限多少？", "每月八十元。", "每次八十元。")
        self.assertEqual(decision.reason_code, "wrong_scope")


if __name__ == "__main__":
    unittest.main()
