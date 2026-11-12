"""Compare direct output with a fail-closed verifier gate on the same drafts."""

import argparse
import json
import math
import statistics
from pathlib import Path


def _latency(values: list[float]) -> dict | None:
    if not values:
        return None
    sorted_values = sorted(values)
    return {"count": len(sorted_values),
            "p50_ms": round(statistics.median(sorted_values), 2),
            "p95_ms_nearest_rank": round(sorted_values[math.ceil(.95 * len(sorted_values)) - 1], 2)}


def compare(report: dict) -> dict:
    rows = report["rows"]
    if len(rows) != report["cases_selected"] or len({row["case"] for row in rows}) != len(rows):
        raise ValueError("REPORT_CASE_SET_INVALID")
    compared = []
    for row in rows:
        raw = row["status"] == "answered"
        checks = row["verifier_checks"]
        verified = (raw and len(checks) == len(row["claims"]) and bool(checks)
                    and all(check["status"] == "supported" for check in checks))
        if raw and not row["claims"]:
            raise ValueError("ANSWER_WITHOUT_CLAIMS")
        if checks and not raw:
            raise ValueError("VERIFIER_WITHOUT_ANSWER")
        expected = row["expected_answerable"]
        compared.append({"case": row["case"], "expected_answerable": expected,
                         "raw_output": raw, "verified_output": verified,
                         "gate_reason": "supported" if verified else
                         ("no_answer" if not raw else
                          "verifier_not_run" if not checks else
                          "verifier_error" if any(check["status"] == "error" for check in checks)
                          else "verifier_rejected"),
                         "raw_ms": row["generation_ms"],
                         "verified_ms": row["elapsed_ms"]})

    def policy_metrics(key: str) -> dict:
        return {"outputs": sum(row[key] for row in compared),
                "answerable_output": sum(row[key] and row["expected_answerable"] for row in compared),
                "unanswerable_output": sum(row[key] and not row["expected_answerable"] for row in compared),
                "answerable_withheld": sum(not row[key] and row["expected_answerable"] for row in compared),
                "unanswerable_withheld": sum(not row[key] and not row["expected_answerable"] for row in compared)}

    called = [row for row, original in zip(compared, rows, strict=True) if original["source_key"]]
    return {"dataset": report["dataset"], "cases": len(compared),
            "raw": policy_metrics("raw_output"),
            "verified": policy_metrics("verified_output"),
            "withheld_by_verifier": [row["case"] for row in compared
                                     if row["raw_output"] and not row["verified_output"]],
            "latency_candidate_requests": {
                "raw_generation": _latency([row["raw_ms"] for row in called]),
                "verified_total": _latency([row["verified_ms"] for row in called]),
            },
            "generator_reported_usage": report.get("generator_reported_usage"),
            "verifier_reported_usage": report.get("verifier_reported_usage"),
            "rows": compared,
            "limitations": "Both policies reuse exactly the same generated drafts. Withheld-on-error is a hypothetical shadow policy, not a product behavior. Expected answerability is a synthetic label, not an independent factual/citation judgment; no verified quality gain is implied."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare(json.loads(args.report.read_text(encoding="utf-8")))
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
