import json
import os
import unittest
from unittest.mock import MagicMock, patch

from eval.generated_answer_pipeline import (GenerationInvalid,
                                            OpenAICitedAnswerGenerator,
                                            generate_answer)


class FakeGenerator:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def complete(self, question, source_text):
        self.calls += 1
        return self.result


class GeneratedAnswerPipelineTests(unittest.TestCase):
    def test_no_source_skips_model_and_cited_claim_builds_answer(self):
        model = FakeGenerator({"claims": [{"text": "每次最多报销一百五十元。",
                                           "evidence_span": "每次最多报销一百五十元"}]})
        self.assertIsNone(generate_answer(model, "最多报多少？", None))
        self.assertEqual(model.calls, 0)
        draft = generate_answer(model, "最多报多少？", "显示器维修每次最多报销一百五十元。")
        self.assertEqual(draft.claims[0].text, "每次最多报销一百五十元。")

    def test_fabricated_span_and_bad_schema_fail_closed(self):
        model = FakeGenerator({"claims": [{"text": "错误答案", "evidence_span": "虚构片段"}]})
        with self.assertRaisesRegex(GenerationInvalid, "MODEL_UNSUPPORTED_SPAN"):
            generate_answer(model, "问题", "真实来源文本。")
        model.result = {"claims": [{"text": "答案", "evidence_span": "真实来源"}], "extra": True}
        with self.assertRaisesRegex(GenerationInvalid, "MODEL_OUTPUT_INVALID"):
            generate_answer(model, "问题", "真实来源文本。")
        model.result = {"claims": []}
        self.assertEqual(generate_answer(model, "问题", "真实来源文本。").claims, [])

    def test_responses_adapter_disables_storage(self):
        client = MagicMock()
        client.responses.create.return_value.output_text = json.dumps({"claims": []})
        with patch("eval.generated_answer_pipeline.create_llm_client", return_value=client), \
                patch.dict(os.environ, {"CITED_ANSWER_API_STYLE": "responses"}):
            model = OpenAICitedAnswerGenerator()
            self.assertEqual(model.complete("问题", "来源"), {"claims": []})
        self.assertFalse(client.responses.create.call_args.kwargs["store"])


if __name__ == "__main__":
    unittest.main()
