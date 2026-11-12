"""Reproducible small routing and authorized retrieval evaluation."""

import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.documents import DocumentRepository
from app.api.intent import high_confidence_intent, knowledge_query, route_intent
from app.api.intent_model import IntentDecision, OpenAIIntentClassifier, focus_supported, retrieval_query
from app.security.policy import Classification, Subject, UserRole


MANAGER = Subject("manager-001", "demo-org", frozenset({"product"}),
                  Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)


def nearest_rank(values, fraction):
    if not values:
        return None
    sorted_values = sorted(values)
    return sorted_values[max(0, int(len(values) * fraction + 0.999999) - 1)]


def evaluate(cases, live, replay=None):
    documents = DocumentRepository(":memory:", seed_demo=True)
    classifier = OpenAIIntentClassifier() if live else None
    replay_rows = {row["id"]: row for row in replay["cases"]} if replay else {}
    rows = []
    try:
        titles = tuple(item.title for item in documents.list_visible(MANAGER))
        for case in cases:
            question = case["question"]
            started = time.perf_counter()
            local = route_intent(question, visible_titles=titles)
            fast = high_confidence_intent(question)
            original_hits = [hit.document.document_id for hit in documents.search_visible(MANAGER, question)]
            model_ms = None
            error = None
            rewritten = None
            focus_terms = ()
            if replay is not None:
                prior = replay_rows[case["id"]]
                intent, rewritten = prior["intent"], prior["search_query"]
                focus_terms = tuple(prior["focus_terms"])
                source, model_ms, error = prior["source"], prior["model_ms"], prior["error_type"]
            elif fast is not None or not live:
                intent = fast if live else local
                source = "local"
            else:
                source = "model"
                model_started = time.perf_counter()
                try:
                    decision = classifier.classify(question, False)
                    intent, rewritten, focus_terms = decision.intent, decision.search_query, decision.focus_terms
                except Exception as exc:
                    intent = local
                    error = type(exc).__name__
                model_ms = round((time.perf_counter() - model_started) * 1000)
            decision = IntentDecision(intent, rewritten, focus_terms)
            query = knowledge_query(retrieval_query(decision, question), None) if intent == "knowledge" else None
            raw_hits = documents.search_visible(MANAGER, query) if query else []
            final_hits = [hit.document.document_id for hit in raw_hits
                          if focus_supported(focus_terms, hit.document.title, hit.text, rewritten)]
            row = {"id": case["id"], "question": question, "expected_intent": case["expected_intent"],
                   "expected_document": case.get("expected_document"), "local_intent": local,
                   "fast_intent": fast, "source": source, "intent": intent,
                   "search_query": rewritten, "focus_terms": list(focus_terms),
                   "retrieval_query": query,
                   "original_hit_ids": original_hits,
                   "raw_hit_ids": [hit.document.document_id for hit in raw_hits],
                   "final_hit_ids": final_hits,
                   "route_ms": prior["route_ms"] if replay is not None else round((time.perf_counter() - started) * 1000),
                   "model_ms": model_ms, "error_type": error}
            rows.append(row)
            print(f"{case['id']} {source} {intent} expected={case['expected_intent']} "
                  f"doc={case.get('expected_document')} hits={final_hits} route_ms={row['route_ms']}", flush=True)
    finally:
        documents.close()
    expected_docs = [row for row in rows if row["expected_document"]]
    model_times = [row["model_ms"] for row in rows if row["model_ms"] is not None]
    summary = {"case_count": len(rows),
               "route_correct": sum(row["intent"] == row["expected_intent"] for row in rows),
               "local_route_correct": sum(row["local_intent"] == row["expected_intent"] for row in rows),
               "document_cases": len(expected_docs),
               "original_document_hit": sum(row["expected_document"] in row["original_hit_ids"] for row in expected_docs),
               "final_document_hit": sum(row["expected_document"] in row["final_hit_ids"] for row in expected_docs),
               "unsupported_knowledge_hits": sum(
                   row["expected_intent"] == "knowledge" and row["expected_document"] is None
                   and bool(row["final_hit_ids"]) for row in rows),
               "model_calls": len(model_times), "model_errors": sum(bool(row["error_type"]) for row in rows),
               "model_median_ms": round(statistics.median(model_times)) if model_times else None,
               "model_max_ms": max(model_times) if model_times else None,
               "model_observed_p90_ms": nearest_rank(model_times, 0.9)}
    return {"recorded_at": datetime.now(timezone.utc).isoformat(),
            "model": replay.get("model") if replay else classifier.model if classifier else None,
            "api_style": replay.get("api_style") if replay else classifier.api_style if classifier else None,
            "summary": summary, "cases": rows,
            "limitations": "Developer-authored synthetic labels and six demo documents. Sequential single requests; observed p90 is descriptive, not a production SLO. Document hit does not prove answer support."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("eval/intent_routing_cases_v1.json"))
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--ids", nargs="*", help="Optional case IDs within the selected split")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay", type=Path, help="Rescore saved model decisions without another model call")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.live and args.replay:
        raise SystemExit("Choose --live or --replay")
    dataset_bytes = args.dataset.read_bytes()
    dataset = json.loads(dataset_bytes.decode("utf-8"))
    cases = [case for case in dataset["cases"] if case["split"] == args.split]
    if args.ids:
        requested = set(args.ids)
        cases = [case for case in cases if case["id"] in requested]
        if {case["id"] for case in cases} != requested:
            raise SystemExit("Unknown case ID for selected split")
    replay = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None
    if replay and (replay.get("split") != args.split or replay.get("dataset_sha256") != hashlib.sha256(dataset_bytes).hexdigest()):
        raise SystemExit("Replay dataset or split mismatch")
    report = evaluate(cases, args.live, replay)
    report["dataset_version"] = dataset["dataset_version"]
    report["dataset_sha256"] = hashlib.sha256(dataset_bytes).hexdigest()
    report["split"] = args.split
    report["selected_ids"] = args.ids
    report["replayed_from"] = str(args.replay) if args.replay else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
