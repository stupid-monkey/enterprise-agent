"""Evaluate model-written cited claims on the frozen synthetic retrieval set."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from eval.answer_support_verifier import (OpenAIEvidenceVerifierModel,
                                          VerificationInvalid, VerifierModel,
                                          verify_answer)
from eval.evaluate_answer_support import REPORT, load_retrieval_inputs
from eval.generated_answer_pipeline import (AnswerGenerator, GenerationInvalid,
                                            OpenAICitedAnswerGenerator,
                                            generate_answer)


def evaluate(report: dict, cases: dict, documents: dict, *,
             generator: AnswerGenerator | None = None,
             verifier: VerifierModel | None = None,
             selected: set[str] | None = None) -> dict:
    by_result = {row["case"]: row for row in report["results"]}
    selected = selected if selected is not None else set(by_result)
    if selected - set(by_result):
        raise ValueError("UNKNOWN_CASE")
    if report["unauthorized_candidates"] or any(row["unauthorized_candidates"] for row in by_result.values()):
        raise ValueError("UNAUTHORIZED_RETRIEVAL_REPORT")
    rows = []
    for name in [row["case"] for row in report["results"] if row["case"] in selected]:
        retrieval = by_result[name]
        case = cases[name]
        source_key = retrieval["gated_key"] if retrieval["quote"] else None
        source = documents[source_key] if source_key else None
        source_text = source["text"] if source else None
        started = time.perf_counter()
        status = "not_run"
        error = None
        claims = []
        generator_usage = None
        verifier_usage = []
        checks = []
        generation_ms = 0.0
        verification_ms = 0.0
        if source_text is None:
            status = "no_source"
        elif generator is not None:
            generation_started = time.perf_counter()
            try:
                draft = generate_answer(generator, case["question"], source_text)
                claims = [claim.model_dump() for claim in draft.claims]
                status = "answered" if claims else "refused"
            except GenerationInvalid as exc:
                status, error = "generation_error", str(exc)
            except Exception as exc:
                status, error = "generation_error", type(exc).__name__
            generation_ms = round((time.perf_counter() - generation_started) * 1000, 2)
            generator_usage = getattr(generator, "last_usage", None)
            if status == "answered" and verifier is not None:
                for claim in claims:
                    verification_started = time.perf_counter()
                    try:
                        decision = verify_answer(verifier, case["question"], claim["text"],
                                                 claim["evidence_span"])
                        checks.append({"status": decision.verdict,
                                       "reason_code": decision.reason_code})
                    except VerificationInvalid as exc:
                        checks.append({"status": "error", "reason_code": str(exc)})
                    except Exception as exc:
                        checks.append({"status": "error", "reason_code": type(exc).__name__})
                    verification_ms += (time.perf_counter() - verification_started) * 1000
                    usage = getattr(verifier, "last_usage", None)
                    if usage:
                        verifier_usage.append(usage)
        verification_ms = round(verification_ms, 2)
        answer = " ".join(claim["text"] for claim in claims)
        expected = case["expected_key"] is not None
        rows.append({
            "case": name, "question": case["question"], "expected_answerable": expected,
            "source_key": source_key, "source_title": source["title"] if source else None,
            "status": status, "error_code": error, "answer": answer, "claims": claims,
            "answerability_match": (status == "answered") == expected
                                   if status not in {"not_run", "generation_error"} else False,
            "verifier_checks": checks,
            "verifier_all_supported": all(check["status"] == "supported" for check in checks)
                                      if checks else None,
            "generator_usage": generator_usage,
            "verifier_usage": verifier_usage,
            "generation_ms": generation_ms, "verification_ms": verification_ms,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        })
    def total_usage(records: list[dict | None]) -> dict | None:
        valid = [item for item in records if item]
        return ({"input_tokens": sum(item["input_tokens"] for item in valid),
                 "output_tokens": sum(item["output_tokens"] for item in valid)}
                if valid else None)
    answered = [row for row in rows if row["status"] == "answered"]
    return {
        "dataset": report["dataset"], "cases_selected": len(rows),
        "generator_calls": sum(row["source_key"] is not None and generator is not None for row in rows),
        "no_source_refusals": sum(row["status"] == "no_source" for row in rows),
        "model_refusals": sum(row["status"] == "refused" for row in rows),
        "answers": len(answered),
        "answerability_matches": sum(row["answerability_match"] for row in rows),
        "generation_errors": sum(row["status"] == "generation_error" for row in rows),
        "verifier_claim_calls": sum(len(row["verifier_checks"]) for row in rows),
        "verifier_supported_answers": sum(row["verifier_all_supported"] is True for row in answered),
        "verifier_rejected_answers": sum(any(check["status"] in {"insufficient", "contradicted"}
                                             for check in row["verifier_checks"]) for row in answered),
        "verifier_error_answers": sum(any(check["status"] == "error"
                                          for check in row["verifier_checks"]) for row in answered),
        "generator_reported_usage": total_usage([row["generator_usage"] for row in rows]),
        "verifier_reported_usage": total_usage([
            usage for row in rows for usage in row["verifier_usage"]]),
        "rows": rows,
        "limitations": "Synthetic sources and questions, inspected retrieval set, and no independent human judgment. Answerability matches do not establish factual correctness; model verifier verdicts require manual review. Provider token counts are unverified. This evaluator does not change the product API.",
    }


def review_cards(report: dict, documents: dict) -> list[dict]:
    """Export unscored cards for a separate reviewer; omit expected labels."""
    return [{"case": row["case"], "question": row["question"],
             "status": row["status"], "answer": row["answer"],
             "claims": row["claims"], "source_title": row["source_title"],
             "source_text": documents[row["source_key"]]["text"] if row["source_key"] else None,
             "human_answer_supported": None, "human_citation_supported": None,
             "human_refusal_appropriate": None,
             "human_notes": ""} for row in report["rows"]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--review-cards", type=Path)
    args = parser.parse_args()
    if args.live and not (args.case or args.all_cases):
        parser.error("--live requires --case or --all-cases")
    if args.verify and not args.live:
        parser.error("--verify requires --live")
    outputs = [path for path in (args.output, args.review_cards) if path is not None]
    if len(set(outputs)) != len(outputs):
        parser.error("--output and --review-cards must use different paths")
    if any(path.exists() for path in outputs):
        parser.error("output already exists; choose new paths to preserve the original run")
    source_report, cases, documents = load_retrieval_inputs(args.report)
    selected = set(args.case) if args.case else None
    generator = OpenAICitedAnswerGenerator() if args.live else None
    verifier = OpenAIEvidenceVerifierModel() if args.verify else None
    result = evaluate(source_report, cases, documents, generator=generator,
                      verifier=verifier, selected=selected)
    result["retrieval_report_sha256"] = hashlib.sha256(args.report.read_bytes()).hexdigest()
    result["generator_model"] = generator.model_name if generator else None
    result["verifier_model"] = verifier.model_name if verifier else None
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    if args.review_cards:
        args.review_cards.write_text(json.dumps(review_cards(result, documents), ensure_ascii=False,
                                                indent=2) + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
