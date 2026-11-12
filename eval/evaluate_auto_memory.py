"""Small, explicit live check of automatic memory extraction on synthetic user text."""

import argparse
import hashlib
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.memory_suggestions import OpenAIMemorySuggester, eligible_auto_memory_text
from app.llm.client import create_llm_client


ROOT = Path(__file__).resolve().parents[1]


def evaluate(cases: dict, *, live: bool, timeout_seconds: float = 25.0) -> dict:
    model = os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini")
    api_style = os.getenv("GENERAL_API_STYLE", "responses")
    suggester = (OpenAIMemorySuggester(create_llm_client(timeout=timeout_seconds, max_retries=0), model, api_style)
                 if live else None)
    rows = []
    for case in cases["cases"]:
        eligible = eligible_auto_memory_text(case["text"])
        started = time.perf_counter()
        error = None
        found = []
        forget = []
        if live and eligible:
            try:
                analysis = suggester.analyze(
                    case["text"], tuple(case.get("context", ())),
                    existing_auto=tuple(tuple(item) for item in case.get("existing_auto", ())))
                found, forget = analysis.memories, analysis.forget
            except Exception as exc:
                error = type(exc).__name__
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        actual = {kind: value for kind, value in found}
        actual_forget = {kind: evidence for kind, evidence in forget}
        expected = set(case["expected_kinds"])
        correct = (error is None and set(actual) == expected
                   and set(actual_forget) == set(case.get("expected_forget", ()))
                   and (not case.get("expect_prefilter") or not eligible)
                   and all(term in actual.get(kind, "")
                           for kind, term in case.get("required_value_terms", {}).items())
                   and all(term not in actual.get(kind, "")
                           for kind, term in case.get("forbidden_value_terms", {}).items())) if live else None
        rows.append({"id": case["id"], "eligible": eligible, "expected_kinds": sorted(expected),
                     "expected_forget": case.get("expected_forget", []),
                     "found": actual, "forget": actual_forget, "correct": correct, "error_type": error,
                     "elapsed_ms": elapsed_ms})
        print(f"{case['id']}: eligible={eligible} found={list(actual)} forget={list(actual_forget)} correct={correct} "
              f"error={error} ms={elapsed_ms}", flush=True)
    calls = [row["elapsed_ms"] for row in rows if row["eligible"] and live]
    return {"dataset_version": cases["version"], "dataset_sha256": hashlib.sha256(
                json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "recorded_at": datetime.now(timezone.utc).isoformat(), "live": live,
            "timeout_seconds": timeout_seconds,
            "model": model if live else None, "api_style": api_style if live else None,
            "summary": {"case_count": len(rows), "correct": sum(row["correct"] is True for row in rows),
                        "errors": sum(row["error_type"] is not None for row in rows),
                        "model_calls": len(calls),
                        "median_model_ms": round(statistics.median(calls)) if calls else None,
                        "max_model_ms": max(calls) if calls else None},
            "cases": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Call the configured model for eligible synthetic cases")
    parser.add_argument("--timeout-seconds", type=float, default=25.0)
    parser.add_argument("--cases", type=Path, default=ROOT / "eval" / "auto_memory_cases_v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "eval" / "auto_memory_v1_result.json")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not 1 <= args.timeout_seconds <= 60:
        raise ValueError("TIMEOUT_OUT_OF_RANGE")
    result = evaluate(cases, live=args.live, timeout_seconds=args.timeout_seconds)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {args.output}", flush=True)


if __name__ == "__main__":
    main()
