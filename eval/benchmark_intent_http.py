"""Measure first SSE event and completion through the local web proxy."""

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


CASES = ("产品发布前有哪些检查项？", "新品发售前要做什么？", "公司有没有住房补贴？",
         "产品发布的概念是什么？", "Python 怎么写文件？")


def request_json(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=headers)
    return urllib.request.urlopen(request, timeout=60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--question", action="append", help="Only probe these question texts")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    password = os.getenv("BENCHMARK_PASSWORD")
    if not password:
        raise SystemExit("Set BENCHMARK_PASSWORD for a local test account")
    with request_json(args.base + "/api/v1/auth/login", {"username": "manager", "password": password}) as response:
        token = json.load(response)["access_token"]
    results = []
    if not 1 <= args.repeat <= 10:
        raise SystemExit("--repeat must be 1..10")
    for question in (tuple(args.question) if args.question else CASES) * args.repeat:
        started = time.perf_counter()
        with request_json(args.base + "/api/v1/conversations/answers/stream", {
                "question": question, "mode": "extract"}, token) as response:
            events = []
            first_ms = None
            for raw in response:
                if not raw.startswith(b"data: "):
                    continue
                event = json.loads(raw[6:])
                if first_ms is None:
                    first_ms = round((time.perf_counter() - started) * 1000)
                events.append(event)
                if event["type"] in ("done", "error"):
                    break
        final = events[-1]
        result = {"question": question, "intent": events[0].get("intent"),
                  "first_event_ms": first_ms, "total_ms": round((time.perf_counter() - started) * 1000),
                  "final_type": final["type"],
                  "answer_status": final.get("answer", {}).get("status"),
                  "citation_count": len(final.get("answer", {}).get("citations", [])),
                  "error_code": final.get("code")}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "base": args.base,
              "repeat": args.repeat,
              "sample_count": len(results), "cases": results,
              "limitations": "One sequential request per synthetic question; first_event_ms includes request and routing; total_ms includes answer generation."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
