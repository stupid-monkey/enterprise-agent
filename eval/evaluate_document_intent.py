"""Measure the current question router's document suggestion on frozen synthetic prompts."""

import argparse
import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.intent import high_confidence_intent, needs_deliverable_classification, route_intent
from app.api.intent_model import IntentDecision, OpenAIIntentClassifier


def nearest_rank(values: list[float], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[math.ceil(fraction * len(ordered)) - 1])


def load_cases(path: Path, split: str) -> tuple[dict, str, list[dict]]:
    source = path.read_bytes()
    dataset = json.loads(source.decode("utf-8"))
    cases = dataset["cases"]
    if not isinstance(cases, list) or not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("INVALID_DOCUMENT_INTENT_CASES")
    for case in cases:
        if (case["split"] not in ("dev", "holdout") or case["expected_intent"] not in ("knowledge", "general")
                or not isinstance(case["expected_document"], bool) or not isinstance(case["question"], str)
                or not 2 <= len(case["question"]) <= 1000):
            raise ValueError("INVALID_DOCUMENT_INTENT_CASES")
    selected = [case for case in cases if case["split"] == split]
    if not selected:
        raise ValueError("EMPTY_DOCUMENT_INTENT_SPLIT")
    return dataset, hashlib.sha256(source).hexdigest(), selected


def summarize(rows: list[dict]) -> dict:
    positive = [row for row in rows if row["expected_document"]]
    negative = [row for row in rows if not row["expected_document"]]
    called = [row["model_ms"] for row in rows if row["model_ms"] is not None]
    return {
        "cases": len(rows),
        "true_positive": sum(row["predicted_document"] for row in positive),
        "false_negative": sum(not row["predicted_document"] for row in positive),
        "true_negative": sum(not row["predicted_document"] for row in negative),
        "false_positive": sum(row["predicted_document"] for row in negative),
        "intent_correct": sum(row["predicted_intent"] == row["expected_intent"] for row in rows),
        "model_calls": len(called),
        "model_errors": sum(row["error_type"] is not None for row in rows),
        "model_p50_ms": round(statistics.median(called)) if called else None,
        "model_p95_nearest_rank_ms": nearest_rank(called, .95),
        "fast_path_count": sum(row["source"] == "fast" for row in rows),
    }


def evaluate(cases: list[dict], classifier_factory) -> tuple[list[dict], dict]:
    rows = []
    for case in cases:
        question = case["question"]
        fast = high_confidence_intent(question)
        call_model = fast is None or (fast == "knowledge" and needs_deliverable_classification(question))
        source = "model" if call_model else "fast"
        model_ms = None
        error_type = None
        if call_model:
            started = time.perf_counter()
            try:
                timeout = 15.0 if needs_deliverable_classification(question) else 8.0
                decision = classifier_factory(timeout=timeout).classify(question, False)
            except Exception as exc:
                decision = IntentDecision(route_intent(question))
                error_type = type(exc).__name__
                source = "fallback"
            model_ms = round((time.perf_counter() - started) * 1000)
        else:
            decision = IntentDecision(fast)
        row = {
            "id": case["id"],
            "expected_intent": case["expected_intent"],
            "predicted_intent": decision.intent,
            "expected_document": case["expected_document"],
            "predicted_document": decision.intent == "knowledge" and decision.document_requested,
            "source": source,
            "model_ms": model_ms,
            "error_type": error_type,
        }
        rows.append(row)
        print(f"{row['id']} {source} document={row['predicted_document']} expected={row['expected_document']} model_ms={model_ms}", flush=True)
    return rows, summarize(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True,
                        help="Explicitly allow calls to the configured model service")
    args = parser.parse_args()
    dataset, digest, cases = load_cases(args.dataset, args.split)
    rows, summary = evaluate(cases, OpenAIIntentClassifier)
    classifier = OpenAIIntentClassifier()
    report = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset["dataset_version"],
        "dataset_sha256": digest,
        "split": args.split,
        "model": classifier.model,
        "api_style": classifier.api_style,
        "summary": summary,
        "rows": rows,
        "limitations": "Developer-authored synthetic labels. This measures routing suggestions before retrieval, ACL and answer generation. Single sequential calls; small-sample p95 is descriptive, not a production SLO. Errors use the local fallback and count as misses when expected_document is true.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
