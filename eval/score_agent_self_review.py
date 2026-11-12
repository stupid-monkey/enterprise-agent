"""Validate and summarize developer-agent review separately from human cards."""

import argparse
import hashlib
import json
from pathlib import Path

from eval.evaluate_answer_support import load_retrieval_inputs


def score(report: dict, review: dict) -> dict:
    if review.get("review_type") != "agent_self_assessment" or review.get("reviewer_independent") is not False:
        raise ValueError("AGENT_REVIEW_PROVENANCE_INVALID")
    original = {row["case"]: row for row in report["rows"]}
    rows = review.get("rows")
    if (not isinstance(rows, list) or len(original) != len(report["rows"])
            or len(rows) != len(original) or len({row.get("case") for row in rows}) != len(rows)
            or {row.get("case") for row in rows} != set(original)):
        raise ValueError("AGENT_REVIEW_CASE_SET_MISMATCH")
    for row in rows:
        status = original[row["case"]]["status"]
        values = [row.get(key) for key in ("answer_supported", "citation_supported",
                                           "citation_context_supported", "refusal_appropriate")]
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("AGENT_REVIEW_REASON_REQUIRED")
        if status == "answered":
            if not all(type(value) is bool for value in values[:3]) or values[3] is not None:
                raise ValueError("AGENT_REVIEW_LABEL_INVALID")
            if row["citation_supported"] and not row["citation_context_supported"]:
                raise ValueError("AGENT_REVIEW_LABEL_INVALID")
        elif status in {"refused", "no_source"}:
            if any(value is not None for value in values[:3]) or type(values[3]) is not bool:
                raise ValueError("AGENT_REVIEW_LABEL_INVALID")
        else:
            raise ValueError("AGENT_REVIEW_UNSCORED_STATUS")
    answered = [row for row in rows if original[row["case"]]["status"] == "answered"]
    refused = [row for row in rows if original[row["case"]]["status"] in {"refused", "no_source"}]
    return {
        "review_type": "agent_self_assessment", "reviewer_independent": False,
        "cases": len(rows), "answered": len(answered), "refused": len(refused),
        "answer_supported": sum(row["answer_supported"] for row in answered),
        "citation_strict_supported": sum(row["citation_supported"] for row in answered),
        "citation_context_supported": sum(row["citation_context_supported"] for row in answered),
        "citation_context_dependent": [row["case"] for row in answered
                                       if not row["citation_supported"] and row["citation_context_supported"]],
        "refusal_appropriate": sum(row["refusal_appropriate"] for row in refused),
        "limitations": "The same development agent authored the dataset and assessed it after seeing model results. This is not independent human review or an unbiased accuracy estimate. The strict citation rubric requires the cited span alone to identify the claim subject and fact; context support additionally allows source title and adjacent source text.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--retrieval-report", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("output already exists")
    report_bytes = args.report.read_bytes()
    retrieval_bytes = args.retrieval_report.read_bytes()
    review_bytes = args.review.read_bytes()
    report = json.loads(report_bytes)
    review = json.loads(review_bytes)
    if (review.get("generated_report_sha256") != hashlib.sha256(report_bytes).hexdigest()
            or review.get("retrieval_report_sha256") != hashlib.sha256(retrieval_bytes).hexdigest()
            or report.get("retrieval_report_sha256") != hashlib.sha256(retrieval_bytes).hexdigest()):
        raise ValueError("AGENT_REVIEW_REPORT_HASH_MISMATCH")
    load_retrieval_inputs(args.retrieval_report)
    result = score(report, review)
    result["review_sha256"] = hashlib.sha256(review_bytes).hexdigest()
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
