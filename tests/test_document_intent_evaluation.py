"""The evaluation scores timeouts and fast-path misses without hiding them."""

import unittest

from app.api.intent_model import IntentDecision
from eval.evaluate_document_intent import evaluate, summarize


class FakeClassifier:
    def __init__(self, result):
        self.result = result

    def classify(self, question, previous_knowledge):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class DocumentIntentEvaluationTests(unittest.TestCase):
    def test_positive_and_negative_model_predictions(self):
        cases = [
            {"id": "positive", "question": "请根据我们公司制度整理一段能给团队看的内容",
             "expected_intent": "knowledge", "expected_document": True},
            {"id": "negative", "question": "年假要准备哪些材料？",
             "expected_intent": "knowledge", "expected_document": False},
        ]
        calls = iter([IntentDecision("knowledge", "年假", ("年假",), True),
                      IntentDecision("knowledge", "年假", ("年假",), False)])
        rows, summary = evaluate(cases, lambda **kwargs: FakeClassifier(next(calls)))
        self.assertEqual([row["predicted_document"] for row in rows], [True, False])
        self.assertEqual((summary["true_positive"], summary["true_negative"]), (1, 1))

    def test_timeout_counts_as_missed_document(self):
        cases = [{"id": "timeout", "question": "请按我们公司制度准备给团队阅读的内容",
                  "expected_intent": "knowledge", "expected_document": True}]
        rows, summary = evaluate(cases, lambda **kwargs: FakeClassifier(TimeoutError()))
        self.assertEqual(rows[0]["source"], "fallback")
        self.assertEqual(summary["model_errors"], 1)
        self.assertEqual(summary["false_negative"], 1)

    def test_fast_path_miss_counts_as_false_negative(self):
        summary = summarize([{ "id": "fast", "source": "fast", "model_ms": None,
                              "error_type": None, "expected_intent": "knowledge",
                              "predicted_intent": "knowledge", "expected_document": True,
                              "predicted_document": False}])
        self.assertEqual(summary["false_negative"], 1)
        self.assertEqual(summary["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
