"""Evaluate the fixed v1 permission-question corpus without a model or product data."""

import argparse
import json
from pathlib import Path

from app.api.file_access_questions import parse_file_access_query


CASES = Path(__file__).with_name("file_access_intent_cases_v1.json")


def evaluate(path: Path = CASES) -> dict:
    cases = json.loads(path.read_text(encoding="utf-8"))
    results = []
    for case in cases:
        parsed = parse_file_access_query(case["question"])
        actual = parsed.action.value if parsed is not None else "none"
        results.append({"question": case["question"], "expected": case["action"],
                        "actual": actual, "correct": actual == case["action"]})
    positive = [item for item in results if item["expected"] != "none"]
    negative = [item for item in results if item["expected"] == "none"]
    true_positive = sum(item["correct"] for item in positive)
    false_positive = sum(item["actual"] != "none" for item in negative)
    return {"cases": len(results), "positive": len(positive), "negative": len(negative),
            "positive_correct": true_positive, "negative_correct": len(negative) - false_positive,
            "positive_recall": round(true_positive / len(positive), 3),
            "negative_specificity": round(1 - false_positive / len(negative), 3),
            "errors": [item for item in results if not item["correct"]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.cases), ensure_ascii=False, indent=2))
