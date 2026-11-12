"""Paired live check that profile helps recall without leaking into a no-profile answer."""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from app.api.general_chat import OpenAIGeneralResponder
from app.llm.client import create_llm_client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    model = os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini")
    style = os.getenv("GENERAL_API_STYLE", "responses")
    responder = OpenAIGeneralResponder(create_llm_client(timeout=30, max_retries=0),
                                       model, style)
    rows = []
    for case in cases["cases"]:
        answers = {}
        for variant, profile in (("without", {}), ("with", case["profile"])):
            started = time.perf_counter()
            try:
                answer = "".join(responder.stream(case["question"], [], profile))
                error = None
            except Exception as exc:
                answer, error = "", type(exc).__name__
            answers[variant] = {"answer": answer, "error_type": error,
                                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
        passed = (all(value["error_type"] is None for value in answers.values())
                  and case["required_with_profile"] in answers["with"]["answer"]
                  and case["forbidden_without_profile"] not in answers["without"]["answer"])
        rows.append({"id": case["id"], "pass": passed, **answers})
        print(f"{case['id']}: pass={passed} with_ms={answers['with']['elapsed_ms']} "
              f"without_ms={answers['without']['elapsed_ms']}", flush=True)
    result = {"dataset_version": cases["version"], "dataset_sha256": hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "recorded_at": datetime.now(timezone.utc).isoformat(), "model": model,
        "api_style": style, "summary": {"pairs": len(rows),
                                      "passed": sum(row["pass"] for row in rows)},
        "cases": rows}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()
