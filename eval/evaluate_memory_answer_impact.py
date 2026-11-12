"""Probe whether synthetic stale or irrelevant long-term memories leak into ordinary answers."""

import hashlib
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.general_chat import OpenAIGeneralResponder
from app.llm.client import create_llm_client


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path,
                        default=ROOT / "eval" / "memory_answer_impact_cases_v1.json")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "eval" / "memory_answer_impact_v1_result.json")
    args = parser.parse_args()
    source = args.cases
    cases = json.loads(source.read_text(encoding="utf-8"))
    model = os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini")
    style = os.getenv("GENERAL_API_STYLE", "responses")
    responder = OpenAIGeneralResponder(create_llm_client(timeout=30, max_retries=0), model, style)
    rows = []
    for case in cases["cases"]:
        started = time.perf_counter()
        try:
            answer = "".join(responder.stream(case["question"], [], case["profile"]))
            error = None
        except Exception as exc:
            answer, error = "", type(exc).__name__
        passed = (error is None and all(term in answer for term in case["required"])
                  and all(term not in answer for term in case["forbidden"]))
        row = {"id": case["id"], "answer": answer, "pass": passed,
               "error_type": error, "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        rows.append(row)
        print(f"{case['id']}: pass={passed} error={error} ms={row['elapsed_ms']}", flush=True)
    result = {"dataset_version": cases["version"], "dataset_sha256": hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "recorded_at": datetime.now(timezone.utc).isoformat(), "model": model, "api_style": style,
        "summary": {"case_count": len(rows), "passed": sum(row["pass"] for row in rows)},
        "cases": rows}
    target = args.output
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {target}", flush=True)


if __name__ == "__main__":
    main()
