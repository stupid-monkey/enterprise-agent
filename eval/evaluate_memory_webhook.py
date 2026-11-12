"""Explicit local webhook delivery drill using a temporary on-host receiver."""

import argparse
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def compose(*arguments: str, env: dict | None = None) -> None:
    subprocess.run(["docker", "compose", *arguments], check=True,
                   capture_output=True, text=True, env=env)


def wait_for(predicate, timeout: float) -> float | None:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if predicate():
            return round(time.perf_counter() - started, 3)
        time.sleep(0.5)
    return None


def drill() -> dict:
    events = []
    lock = threading.Lock()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            event = json.loads(data)
            with lock:
                events.append(event)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("0.0.0.0", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    webhook_env = dict(os.environ)
    webhook_env["MEMORY_MONITOR_WEBHOOK_URL"] = (
        f"http://host.docker.internal:{server.server_port}/alert")
    default_env = dict(os.environ)
    default_env.pop("MEMORY_MONITOR_WEBHOOK_URL", None)
    database_stopped = False
    try:
        compose("--profile", "monitor", "up", "-d", "--no-deps",
                "--force-recreate", "memory-monitor", env=webhook_env)
        initial_seconds = wait_for(
            lambda: any(item.get("event") == "MEMORY_RECOVERED" for item in events), 25)
        if initial_seconds is None:
            raise RuntimeError("WEBHOOK_RECEIVER_UNREACHABLE")
        with lock:
            events.clear()
        compose("stop", "memory-store")
        database_stopped = True
        alert_seconds = wait_for(
            lambda: any(item.get("event") == "MEMORY_ALERT"
                        and "postgres_unavailable" in item.get("reasons", [])
                        for item in events), 30)
        compose("--profile", "memory-store", "up", "-d", "memory-store")
        database_stopped = False
        recovered_seconds = wait_for(
            lambda: any(item.get("event") == "MEMORY_RECOVERED" for item in events), 30)
        with lock:
            copied = list(events)
        result = {"dataset": "local-memory-webhook-drill-v1",
                  "initial_delivery_seconds": initial_seconds,
                  "alert_delivery_seconds": alert_seconds,
                  "recovery_delivery_seconds": recovered_seconds,
                  "events": [item.get("event") for item in copied],
                  "verified": alert_seconds is not None and recovered_seconds is not None}
        if not result["verified"]:
            raise RuntimeError("WEBHOOK_DRILL_FAILED:" + json.dumps(result))
        return result
    finally:
        if database_stopped:
            compose("--profile", "memory-store", "up", "-d", "memory-store")
        compose("--profile", "monitor", "up", "-d", "--no-deps",
                "--force-recreate", "memory-monitor", env=default_env)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
