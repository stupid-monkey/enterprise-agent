"""Score human-filled review cards without using model verdicts as labels."""

import argparse
import hashlib
import json
from pathlib import Path

from eval.evaluate_answer_support import REPORT, load_retrieval_inputs


def score(report: dict, cards: list[dict], documents: dict | None = None) -> dict:
    original = {row["case"]: row for row in report["rows"]}
    if (len(original) != len(report["rows"]) or len(cards) != len(original)
            or len({card.get("case") for card in cards}) != len(cards)
            or {card.get("case") for card in cards} != set(original)):
        raise ValueError("REVIEW_CASE_SET_MISMATCH")
    rows = []
    for card in cards:
        source = original[card["case"]]
        if any(card.get(key) != source[key] for key in
               ("question", "status", "answer", "claims", "source_title")):
            raise ValueError("REVIEW_CONTENT_CHANGED")
        if documents is not None:
            expected_text = documents[source["source_key"]]["text"] if source["source_key"] else None
            if card.get("source_text") != expected_text:
                raise ValueError("REVIEW_SOURCE_CHANGED")
        values = [card.get(key) for key in ("human_answer_supported",
                                            "human_citation_supported",
                                            "human_refusal_appropriate")]
        if any(value is not None and type(value) is not bool for value in values):
            raise ValueError("REVIEW_LABEL_INVALID")
        if source["status"] == "answered":
            reviewed = values[0] is not None and values[1] is not None
            if values[2] is not None:
                raise ValueError("REVIEW_LABEL_INVALID")
        elif source["status"] in {"refused", "no_source"}:
            reviewed = values[2] is not None
            if values[0] is not None or values[1] is not None:
                raise ValueError("REVIEW_LABEL_INVALID")
        else:
            reviewed = False
            if any(value is not None for value in values):
                raise ValueError("REVIEW_LABEL_INVALID")
        rows.append({"case": card["case"], "status": source["status"],
                     "reviewed": reviewed, "answer_supported": values[0],
                     "citation_supported": values[1], "refusal_appropriate": values[2]})
    answered = [row for row in rows if row["status"] == "answered" and row["reviewed"]]
    refused = [row for row in rows if row["status"] in {"refused", "no_source"} and row["reviewed"]]
    return {"cases": len(rows), "reviewed": sum(row["reviewed"] for row in rows),
            "pending": [row["case"] for row in rows if not row["reviewed"]],
            "answered_reviewed": len(answered),
            "answer_supported": sum(row["answer_supported"] for row in answered),
            "citation_supported": sum(row["citation_supported"] for row in answered),
            "refusals_reviewed": len(refused),
            "refusal_appropriate": sum(row["refusal_appropriate"] for row in refused),
            "rows": rows,
            "limitations": "Human labels must be entered by a reviewer. Empty labels are not counted as success. Review cards omit expected labels but may still be familiar to the evaluator."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cards", type=Path, required=True)
    parser.add_argument("--retrieval-report", type=Path, default=REPORT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    generated = json.loads(args.report.read_text(encoding="utf-8"))
    if hashlib.sha256(args.retrieval_report.read_bytes()).hexdigest() != generated["retrieval_report_sha256"]:
        raise ValueError("RETRIEVAL_REPORT_MISMATCH")
    _, cases, documents = load_retrieval_inputs(args.retrieval_report)
    cards = json.loads(args.cards.read_text(encoding="utf-8"))
    result = score(generated, cards, documents)
    if any("review_context" in card for card in cards):
        from eval.prepare_generated_answer_review import validate_context
        validate_context(generated, cards, cases, documents)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
