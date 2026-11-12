"""One-shot synthetic check of model proposals and the resulting memory state."""

import argparse
import hashlib
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.memory_suggestions import OpenAIMemorySuggester, eligible_auto_memory_text
from app.api.user_memories import UserMemoryRepository
from app.llm.client import create_llm_client
from app.security.policy import Classification, Subject


ROOT = Path(__file__).resolve().parents[1]


def evaluate(cases: dict) -> dict:
    for case in cases["cases"]:
        prior = case["prior"]
        if (prior["origin"] == "auto"
                and (prior["value"] not in prior["source_text"]
                     or not eligible_auto_memory_text(prior["source_text"]))):
            raise ValueError(f"INVALID_PRIOR_MEMORY_CASE:{case['id']}")
    model = os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini")
    api_style = os.getenv("GENERAL_API_STYLE", "responses")
    suggester = OpenAIMemorySuggester(create_llm_client(timeout=25, max_retries=0),
                                      model, api_style)
    subject = Subject("synthetic-user", "synthetic-org", frozenset(), Classification.INTERNAL)
    rows = []
    for case in cases["cases"]:
        repository = UserMemoryRepository(":memory:", default_auto_suggest=True)
        started = time.perf_counter()
        try:
            prior = case["prior"]
            if prior["origin"] == "manual":
                repository.upsert(subject, prior["kind"], prior["value"])
            else:
                repository.upsert_auto(subject, prior["kind"], prior["value"],
                                       "prior-conversation", prior["source_text"])
            snapshot = repository.list_for(subject)[0]
            existing_auto = ((snapshot["kind"], snapshot["value"]),) if (
                snapshot["kind"] in ("role", "task") and snapshot["origin"] == "auto") else ()
            error = None
            memories, forget = (), ()
            try:
                analysis = suggester.analyze(case["text"], existing_auto=existing_auto)
                memories, forget = analysis.memories, analysis.forget
                for kind, evidence in forget:
                    if kind == "task" and not any(item_kind == kind for item_kind, _ in memories):
                        repository.forget_auto_task_cross_session(
                            subject, snapshot, "latest-conversation", case["text"], evidence)
                    if kind == "role" and not any(item_kind == kind for item_kind, _ in memories):
                        repository.forget_auto_role_cross_session(
                            subject, snapshot, "latest-conversation", case["text"], evidence)
                for kind, value in memories:
                    repository.upsert_auto(subject, kind, value, "latest-conversation", case["text"])
            except Exception as exc:
                error = type(exc).__name__
            after = next((item for item in repository.list_for(subject)
                          if item["kind"] == prior["kind"]), None)
            if after is None:
                effect = "deleted"
            elif after["value"] == snapshot["value"]:
                effect = "kept"
            else:
                effect = "replaced"
            correct = (error is None and effect == case["expected_effect"]
                       and (after is None or all(term in after["value"] for term in
                                                case.get("required_after_terms", ()))))
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            rows.append({"id": case["id"], "expected_effect": case["expected_effect"],
                         "actual_effect": effect, "correct": correct,
                         "model_memories": [kind for kind, _ in memories],
                         "model_forget": [kind for kind, _ in forget],
                         "error_type": error, "elapsed_ms": elapsed_ms})
            print(f"{case['id']}: expected={case['expected_effect']} actual={effect} "
                  f"correct={correct} error={error} ms={elapsed_ms}", flush=True)
        finally:
            repository.close()
    durations = [row["elapsed_ms"] for row in rows]
    return {"dataset_version": cases["version"], "dataset_sha256": hashlib.sha256(
                json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "recorded_at": datetime.now(timezone.utc).isoformat(), "model": model,
            "api_style": api_style,
            "summary": {"case_count": len(rows), "correct": sum(row["correct"] for row in rows),
                        "errors": sum(row["error_type"] is not None for row in rows),
                        "median_ms": round(statistics.median(durations)) if durations else None,
                        "max_ms": max(durations) if durations else None}, "cases": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "eval" / "memory_conflict_cases_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "eval" / "memory_conflict_v1_result.json")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    result = evaluate(cases)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
