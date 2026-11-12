"""Evaluate proposed-answer support against already authorized shadow quotes."""

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from eval.answer_support_verifier import (OpenAIEvidenceVerifierModel,
                                          VerificationInvalid, VerifierModel, verify_answer)
from eval.evaluate_pg_hybrid_independent import SUITES


ROOT = Path(__file__).parent
REPORT = ROOT / "pg_hybrid_stress_v3_first_2026_10_30.json"
CLAIMS = ROOT / "verification_claims_v3.json"


def load_retrieval_inputs(report_path: Path = REPORT) -> tuple[dict, dict, dict]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    document_path, case_path = SUITES[report["dataset"]]
    if (hashlib.sha256(document_path.read_bytes()).hexdigest() != report["document_sha256"]
            or hashlib.sha256(case_path.read_bytes()).hexdigest() != report["case_sha256"]):
        raise ValueError("DATASET_HASH_MISMATCH")
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    documents = {item["key"]: item for item in json.loads(document_path.read_text(encoding="utf-8"))}
    by_case = {item["name"]: item for item in cases}
    by_result = {item["case"]: item for item in report["results"]}
    names = set(by_case)
    if (len(names) != len(cases) or len(by_result) != len(report["results"])
            or names != set(by_result)):
        raise ValueError("CASE_SET_MISMATCH")
    for name in names:
        result = by_result[name]
        if result["expected_key"] != by_case[name]["expected_key"]:
            raise ValueError("CASE_LABEL_MISMATCH")
        source_key = result["gated_key"]
        quote = result["quote"]
        if quote:
            if source_key not in documents:
                raise ValueError("SOURCE_NOT_FOUND")
            source_text = documents[source_key]["text"]
            parts = [part.strip() for part in re.split(r"(?<=[。！？；;])\s*", quote) if part.strip()]
            if not parts or any(part not in source_text for part in parts):
                raise ValueError("QUOTE_PROVENANCE_INVALID")
        elif source_key is not None:
            raise ValueError("SOURCE_WITHOUT_QUOTE")
    return report, by_case, documents


def load_inputs(report_path: Path = REPORT, claim_path: Path = CLAIMS) -> tuple[dict, list[dict], dict, dict]:
    report, by_case, documents = load_retrieval_inputs(report_path)
    claims = json.loads(claim_path.read_text(encoding="utf-8"))
    by_claim = {item["case"]: item for item in claims}
    if len(by_claim) != len(claims) or set(by_claim) != set(by_case):
        raise ValueError("CASE_SET_MISMATCH")
    for name, claim in by_claim.items():
        if (type(claim.get("supported")) is not bool or claim["supported"] != bool(by_case[name]["expected_key"])
                or not isinstance(claim.get("answer"), str) or not claim["answer"].strip()
                or len(claim["answer"]) > 1000):
            raise ValueError("CLAIM_LABEL_INVALID")
    return report, claims, by_case, documents


def evaluate(report: dict, claims: list[dict], cases: dict, documents: dict, *, model: VerifierModel | None = None,
             selected: set[str] | None = None) -> dict:
    by_result = {item["case"]: item for item in report["results"]}
    selected = selected or {item["case"] for item in claims}
    if selected - set(by_result):
        raise ValueError("UNKNOWN_CASE")
    rows = []
    for claim in claims:
        name = claim["case"]
        if name not in selected:
            continue
        result = by_result[name]
        quote = result["quote"]
        source_text = documents[result["gated_key"]]["text"] if quote else None
        started = time.perf_counter()
        status = "not_run"
        reason = None
        usage = None
        if not quote:
            status = "insufficient"
            reason = "no_quote"
        elif model is not None:
            try:
                decision = verify_answer(model, cases[name]["question"], claim["answer"], source_text)
                status = decision.verdict
                reason = decision.reason_code
            except VerificationInvalid as exc:
                status, reason = "error", str(exc)
            except Exception as exc:
                status, reason = "error", type(exc).__name__
            usage = getattr(model, "last_usage", None)
        rows.append({"case": name, "expected_supported": claim["supported"],
                     "status": status, "reason_code": reason,
                     "correct": (status == "supported") == claim["supported"]
                                if status not in {"not_run", "error"} else False,
                     "model_called": bool(quote and model is not None),
                     "usage": usage, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
    measured = [row for row in rows if row["status"] not in {"not_run", "error"}]
    usage_rows = [row["usage"] for row in rows if row["usage"]]
    return {"dataset": report["dataset"], "cases_selected": len(rows),
            "model_calls": sum(row["model_called"] for row in rows),
            "measured": len(measured), "correct": sum(row["correct"] for row in measured),
            "positive_supported": sum(row["status"] == "supported" and row["expected_supported"]
                                      for row in measured),
            "negative_rejected": sum(row["status"] in {"insufficient", "contradicted"}
                                     and not row["expected_supported"] for row in measured),
            "errors": sum(row["status"] == "error" for row in rows),
            "reported_usage": {"input_tokens": sum(row["input_tokens"] for row in usage_rows),
                               "output_tokens": sum(row["output_tokens"] for row in usage_rows)}
                              if usage_rows else None,
            "rows": rows,
            "limitations": "Hand-authored claims on synthetic questions and previously inspected retrieval results. The verifier sees the complete frozen source text; selected quote sentences are provenance checked, but may be noncontiguous. Provider token counts are unverified; no price or generated-answer quality claim. Model verdicts are not authoritative ACL or entailment proof."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--claims", type=Path, default=CLAIMS)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report, claims, cases, documents = load_inputs(args.report, args.claims)
    if args.live and not (args.case or args.all_cases):
        parser.error("--live requires --case or --all-cases")
    selected = {item["case"] for item in claims} if args.all_cases or not args.case else set(args.case)
    model = OpenAIEvidenceVerifierModel() if args.live else None
    result = evaluate(report, claims, cases, documents, model=model, selected=selected)
    result["claim_sha256"] = hashlib.sha256(args.claims.read_bytes()).hexdigest()
    result["model"] = model.model_name if model else None
    result["api_style"] = model.api_style if model else None
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
