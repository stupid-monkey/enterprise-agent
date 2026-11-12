"""Compare repeated live answer-generation reports on the same frozen cases."""

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


def _latency(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {"count": len(ordered), "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms_nearest_rank": round(ordered[math.ceil(.95 * len(ordered)) - 1], 2)}


def compare(reports: list[dict]) -> dict:
    if len(reports) < 2:
        raise ValueError("AT_LEAST_TWO_REPORTS_REQUIRED")
    first = reports[0]
    names = [row["case"] for row in first["rows"]]
    if len(set(names)) != len(names) or len(names) != first["cases_selected"]:
        raise ValueError("REPORT_CASE_SET_INVALID")
    required = ("dataset", "retrieval_report_sha256", "generator_model", "verifier_model")
    for report in reports:
        current = [row["case"] for row in report["rows"]]
        if (any(report.get(key) != first.get(key) for key in required)
                or len(current) != report["cases_selected"] or set(current) != set(names)
                or len(set(current)) != len(current)):
            raise ValueError("REPEAT_INPUT_MISMATCH")
    runs = [{row["case"]: row for row in report["rows"]} for report in reports]
    cases = []
    for name in names:
        rows = [run[name] for run in runs]
        statuses = [row["status"] for row in rows]
        answers = [row["answer"] for row in rows]
        verdicts = ["not_applicable" if row["status"] != "answered" else
                    "supported" if row["verifier_all_supported"] is True else
                    "not_run" if not row["verifier_checks"] else
                    "error" if any(check["status"] == "error" for check in row["verifier_checks"]) else
                    "rejected" for row in rows]
        cases.append({"case": name, "statuses": statuses, "answers": answers,
                      "verifier_outcomes": verdicts,
                      "status_stable": len(set(statuses)) == 1,
                      "answer_stable": len(set(answers)) == 1,
                      "verifier_comparable": all(status == "answered" for status in statuses),
                      "verifier_stable": (all(status == "answered" for status in statuses)
                                          and len(set(verdicts)) == 1)})
    return {
        "dataset": first["dataset"], "case_count": len(names), "run_count": len(reports),
        "retrieval_report_sha256": first["retrieval_report_sha256"],
        "generator_model": first["generator_model"],
        "verifier_model": first["verifier_model"],
        "status_stable_cases": sum(row["status_stable"] for row in cases),
        "answer_stable_cases": sum(row["answer_stable"] for row in cases),
        "verifier_comparable_cases": sum(row["verifier_comparable"] for row in cases),
        "verifier_stable_answered_cases": sum(row["verifier_stable"] for row in cases),
        "generation_error_count": sum(row["status"] == "generation_error"
                                      for report in reports for row in report["rows"]),
        "verifier_error_answer_count": sum(any(check["status"] == "error"
                                               for check in row["verifier_checks"])
                                           for report in reports for row in report["rows"]),
        "candidate_request_latency_by_run": [
            _latency([row["elapsed_ms"] for row in report["rows"] if row["source_key"]])
            for report in reports],
        "provider_reported_usage_by_run": [
            {"generator": report.get("generator_reported_usage"),
             "verifier": report.get("verifier_reported_usage")}
            for report in reports],
        "cases": cases,
        "limitations": "Exact answer string agreement is a strict stability measure, not factual correctness. Small sequential synthetic runs do not estimate production failure rates, concurrency p95, or human citation support. Provider usage is unverified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = [path.read_bytes() for path in args.reports]
    result = compare([json.loads(item) for item in raw])
    result["input_report_sha256"] = [hashlib.sha256(item).hexdigest() for item in raw]
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        if args.output.exists():
            raise ValueError("OUTPUT_ALREADY_EXISTS")
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
