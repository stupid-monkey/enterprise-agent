"""Verify that the memory watchdog starts and alerts while the API is down.

This drill briefly stops the local demo API. It always starts the API again.
"""

import argparse
import http.client
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


def api_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/readyz", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return False


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
    if not api_ready():
        raise RuntimeError("API_NOT_READY_BEFORE_DRILL")
    if "memory-monitor" not in compose("ps", "--services", "--status", "running"):
        raise RuntimeError("MEMORY_MONITOR_NOT_RUNNING")
    stopped = False
    started_at = datetime.now(timezone.utc)
    try:
        compose("stop", "api")
        stopped = True
        compose("--profile", "monitor", "up", "-d", "--no-deps",
                "--force-recreate", "memory-monitor")
        alert_seconds = wait_for(
            lambda: any(event.get("event") == "MEMORY_ALERT"
                        and "api_unready" in event.get("reasons", [])
                        for event in monitor_events(started_at)), 30)
        if alert_seconds is None:
            raise RuntimeError("MONITOR_DID_NOT_ALERT_WITH_API_DOWN")
    finally:
        if stopped:
            compose("up", "-d", "--no-deps", "api")
    api_recover_seconds = wait_for(api_ready, 45)
    monitor_recover_seconds = wait_for(
        lambda: any(event.get("event") == "MEMORY_RECOVERED"
                    for event in monitor_events(started_at)), 30)
    result = {"dataset": "local-memory-monitor-bootstrap-v1",
              "alert_seconds": alert_seconds,
              "api_recover_seconds": api_recover_seconds,
              "monitor_recover_seconds": monitor_recover_seconds,
              "verified": all(value is not None for value in (
                  alert_seconds, api_recover_seconds, monitor_recover_seconds))}
    if not result["verified"]:
        raise RuntimeError("MONITOR_BOOTSTRAP_DRILL_FAILED:" + json.dumps(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
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
