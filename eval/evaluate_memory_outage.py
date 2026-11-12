"""Explicit local Docker outage drill for API readiness and memory watchdog.

Run with --run only in the dedicated local demo environment. The database
service is restarted in a finally block even when an assertion fails.
"""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def compose(*arguments: str) -> str:
    return subprocess.run(["docker", "compose", *arguments], check=True,
                          capture_output=True, text=True).stdout


def ready_status() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/readyz", timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError):
        return 0


def monitor_events(since: datetime) -> list[dict]:
    output = compose("logs", "--no-color", "--tail", "100", "memory-monitor")
    events = []
    for line in output.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(line[start:])
            if datetime.fromisoformat(event["at"]) >= since:
                events.append(event)
        except (ValueError, KeyError, TypeError):
            continue
    return events


def wait_for(predicate, timeout: float) -> float | None:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if predicate():
            return round(time.perf_counter() - started, 3)
        time.sleep(0.5)
    return None


def drill() -> dict:
    if ready_status() != 200:
        raise RuntimeError("API_NOT_READY_BEFORE_DRILL")
    if "memory-monitor" not in compose("ps", "--services", "--status", "running"):
        raise RuntimeError("MEMORY_MONITOR_NOT_RUNNING")
    started_at = datetime.now(timezone.utc)
    database_stopped = False
    try:
        compose("stop", "memory-store")
        database_stopped = True
        api_detect_seconds = wait_for(lambda: ready_status() != 200, 20)
        monitor_detect_seconds = wait_for(
            lambda: any(event.get("event") == "MEMORY_ALERT"
                        and "postgres_unavailable" in event.get("reasons", [])
                        for event in monitor_events(started_at)), 20)
        time.sleep(3)
    finally:
        if database_stopped:
            compose("--profile", "memory-store", "up", "-d", "memory-store")
    api_recover_seconds = wait_for(lambda: ready_status() == 200, 30)
    monitor_recover_seconds = wait_for(
        lambda: any(event.get("event") == "MEMORY_RECOVERED"
                    for event in monitor_events(started_at)), 30)
    events = monitor_events(started_at)
    result = {"dataset": "local-docker-memory-outage-v1",
              "api_detect_seconds": api_detect_seconds,
              "monitor_detect_seconds": monitor_detect_seconds,
              "api_recover_seconds": api_recover_seconds,
              "monitor_recover_seconds": monitor_recover_seconds,
              "alert_reasons": sorted({reason for event in events
                                       if event.get("event") == "MEMORY_ALERT"
                                       for reason in event.get("reasons", [])}),
              "verified": all(value is not None for value in (
                  api_detect_seconds, monitor_detect_seconds,
                  api_recover_seconds, monitor_recover_seconds))}
    if not result["verified"]:
        raise RuntimeError("MEMORY_OUTAGE_DRILL_FAILED:" + json.dumps(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Stop and restart local memory-store")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        raise ValueError("EXPLICIT_RUN_FLAG_REQUIRED")
    result = drill()
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
