"""Replay a recorded model run through the current local permission gate."""

import argparse
import hashlib
import json
from pathlib import Path

from app.api.file_access_questions import (model_permission_fallback_allowed,
                                           parse_file_access_query)


def evaluate(cases_path: Path, model_path: Path) -> dict:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    model_result = json.loads(model_path.read_text(encoding="utf-8"))
    if model_result.get("corpus_sha256") != hashlib.sha256(cases_path.read_bytes()).hexdigest():
        raise ValueError("MODEL_RECORD_CORPUS_MISMATCH")
    recorded = model_result["rows"]
    if len(cases) != len(recorded):
        raise ValueError("MODEL_RECORD_COUNT_MISMATCH")
    labels = {"read": "permission_read", "download": "permission_download", "none": "answer"}
    results = []
    for case, model in zip(cases, recorded):
        expected = labels[case["action"]]
        if model["expected"] != expected:
            raise ValueError("MODEL_RECORD_LABEL_MISMATCH")
        local = parse_file_access_query(case["question"])
        if local is not None:
            predicted = labels[local.action.value]
            route = "local"
        elif (model["predicted"] in ("permission_read", "permission_download")
              and model_permission_fallback_allowed(case["question"])):
            predicted = model["predicted"]
            route = "model_permission"
        else:
            predicted = "answer"
            route = "ordinary_file_answer"
        results.append({"expected": expected, "predicted": predicted,
                        "route": route, "correct": predicted == expected})
    positives = [item for item in results if item["expected"] != "answer"]
    negatives = [item for item in results if item["expected"] == "answer"]
    return {"cases": len(results),
            "positive_correct": sum(item["correct"] for item in positives),
            "positive_count": len(positives),
            "negative_correct": sum(item["correct"] for item in negatives),
            "negative_count": len(negatives),
            "local_routes": sum(item["route"] == "local" for item in results),
            "model_permission_routes": sum(item["route"] == "model_permission" for item in results),
            "errors": [item for item in results if not item["correct"]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.cases, args.model), ensure_ascii=False, indent=2))
