"""Score the selected-file intent model on synthetic, frozen permission prompts."""

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from app.api.collaboration import OpenAICollaborationModel
from app.llm.client import MODEL_NAME


def evaluate(cases_path: Path) -> dict:
    source = cases_path.read_bytes()
    cases = json.loads(source.decode("utf-8"))
    model = OpenAICollaborationModel()
    mapping = {"read": "permission_read", "download": "permission_download", "none": "answer"}
    rows = []
    for case in cases:
        started = time.monotonic()
        try:
            result = model.complete("file_intent", {"question": case["question"],
                                                    "file_title": "安全规范"})
            predicted = result.get("intent")
            error = None
        except Exception as exc:
            predicted = None
            error = type(exc).__name__
        latency = round((time.monotonic() - started) * 1000)
        expected = mapping[case["action"]]
        rows.append({"expected": expected, "predicted": predicted,
                     "correct": predicted == expected, "latency_ms": latency,
                     **({"error_type": error} if error else {})})
    latencies = sorted(row["latency_ms"] for row in rows)
    positive = [row for row in rows if row["expected"] != "answer"]
    negative = [row for row in rows if row["expected"] == "answer"]
    return {"corpus_sha256": hashlib.sha256(source).hexdigest(),
            "model": MODEL_NAME,
            "cases": len(rows), "positive_correct": sum(row["correct"] for row in positive),
            "positive_count": len(positive), "negative_correct": sum(row["correct"] for row in negative),
            "negative_count": len(negative), "errors": sum("error_type" in row for row in rows),
            "latency_p50_ms": round(statistics.median(latencies)),
            "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
            "rows": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.cases)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "rows"},
                     ensure_ascii=False))
