"""Small, explicit live routing probe; run with the configured synthetic questions only."""

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.intent import route_intent
from app.api.intent_model import OpenAIIntentClassifier


CASES = (
    ("新品发售前要做什么？", "knowledge"),
    ("员工休假额度如何算？", "knowledge"),
    ("项目启动会应该准备哪些材料？", "knowledge"),
    ("Python 怎么写文件？", "general"),
    ("什么是公司法？", "general"),
    ("如何写周报？", "general"),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = OpenAIIntentClassifier()
    results = []
    for question, expected in CASES:
        started = time.perf_counter()
        local = route_intent(question)
        local_ms = round((time.perf_counter() - started) * 1000, 3)
        started = time.perf_counter()
        try:
            decision = model.classify(question, False)
            predicted = decision.intent
            search_query = decision.search_query
            error = None
        except Exception as exc:
            predicted = None
            search_query = None
            error = type(exc).__name__
        model_ms = round((time.perf_counter() - started) * 1000)
        results.append({"question": question, "expected": expected, "local": local,
                        "local_ms": local_ms, "model": predicted, "search_query": search_query,
                        "model_ms": model_ms,
                        "error_type": error})
        print(f"{question} local={local} {local_ms}ms model={predicted or error} {model_ms}ms")
    successful = [item for item in results if item["model"] is not None]
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "model": model.model,
              "api_style": model.api_style, "sample_count": len(CASES),
              "correct": sum(item["model"] == item["expected"] for item in results),
              "errors": len(CASES) - len(successful),
              "median_model_ms": round(statistics.median(item["model_ms"] for item in successful)) if successful else None,
              "cases": results,
              "limitations": "Six handpicked single-run synthetic questions; not p95 or production accuracy."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
