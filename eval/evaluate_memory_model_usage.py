"""Small live sample of provider-reported memory extraction token usage."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from app.api.memory_suggestions import OpenAIMemorySuggester
from app.llm.client import create_llm_client


class ResponsesRecorder:
    def __init__(self, actual):
        self.actual = actual
        self.usage = None

    def create(self, **kwargs):
        response = self.actual.create(**kwargs)
        usage = getattr(response, "usage", None)
        self.usage = ({"input_tokens": getattr(usage, "input_tokens", None),
                       "output_tokens": getattr(usage, "output_tokens", None),
                       "total_tokens": getattr(usage, "total_tokens", None)}
                      if usage is not None else None)
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    chosen = {"v6_role_plain", "v6_task_handoff", "v6_preference_plain",
              "v6_public_question", "v6_task_progress"}
    client = create_llm_client(timeout=25, max_retries=0)
    recorder = ResponsesRecorder(client.responses)
    proxy = type("ClientProxy", (), {"responses": recorder})()
    model = os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini")
    suggester = OpenAIMemorySuggester(proxy, model, "responses")
    rows = []
    for case in cases["cases"]:
        if case["id"] not in chosen:
            continue
        recorder.usage = None
        began = time.perf_counter()
        try:
            analysis = suggester.analyze(
                case["text"], tuple(case.get("context", ())),
                existing_auto=tuple(tuple(item) for item in case.get("existing_auto", ())))
            error = None
            found = [kind for kind, _ in analysis.memories]
        except Exception as exc:
            error, found = type(exc).__name__, []
        rows.append({"id": case["id"], "found_kinds": found,
                     "error_type": error, "elapsed_ms": round((time.perf_counter() - began) * 1000),
                     "provider_reported_usage": recorder.usage})
    result = {"dataset": "memory-model-usage-sample-v1", "model": model,
              "sample_count": len(rows), "source_cases_sha256": hashlib.sha256(
                  json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
              "notes": "Usage is reported by a third-party OpenAI-compatible endpoint; prices and billing are not independently verified.",
              "cases": rows}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"sample_count": len(rows),
                      "errors": sum(row["error_type"] is not None for row in rows),
                      "usage_reported": sum(row["provider_reported_usage"] is not None
                                            for row in rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
