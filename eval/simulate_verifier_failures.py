"""Offline sensitivity check for verifier outages on already generated drafts.

No model calls are made. Injected failures change release decisions only; the
recorded timings and token usage from the original run are not simulated.
"""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from eval.compare_answer_policies import compare


def simulate(report: dict) -> dict:
    baseline = compare(report)
    eligible = [row["case"] for row in baseline["rows"] if row["verified_output"]]
    single_failures = []
    for case in eligible:
        altered = copy.deepcopy(report)
        target = next(row for row in altered["rows"] if row["case"] == case)
        target["verifier_checks"][0] = {"status": "error", "reason_code": "injected_outage"}
        outcome = compare(altered)
        single_failures.append({
            "case": case,
            "direct_outputs": outcome["raw"]["outputs"],
            "verified_outputs": outcome["verified"]["outputs"],
            "withheld_by_verifier": outcome["withheld_by_verifier"],
        })

    all_failed = copy.deepcopy(report)
    for row in all_failed["rows"]:
        if row["status"] == "answered":
            row["verifier_checks"] = [{"status": "error", "reason_code": "injected_outage"}
                                      for _ in row["claims"]]
    outage = compare(all_failed)
    return {
        "dataset": report["dataset"],
        "cases": baseline["cases"],
        "baseline_direct_outputs": baseline["raw"]["outputs"],
        "baseline_verified_outputs": baseline["verified"]["outputs"],
        "eligible_verified_answers": len(eligible),
        "single_failure_scenarios": single_failures,
        "all_verifiers_fail": {
            "direct_outputs": outage["raw"]["outputs"],
            "verified_outputs": outage["verified"]["outputs"],
            "answerable_withheld": outage["verified"]["answerable_withheld"],
            "withheld_by_verifier": outage["withheld_by_verifier"],
        },
        "limitations": "Synthetic release-decision fault injection only. No model rerun, retry, actual outage latency, quality judgment, or production fallback is measured.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report_bytes = args.report.read_bytes()
    result = simulate(json.loads(report_bytes))
    result["input_report_sha256"] = hashlib.sha256(report_bytes).hexdigest()
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
