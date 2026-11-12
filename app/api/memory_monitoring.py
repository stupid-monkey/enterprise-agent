"""Independent local watchdog for the PostgreSQL memory cutover.

It writes aggregate alert transitions to container logs; it never reads or logs
conversation text, account IDs, profile values, or database credentials.
"""

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql


def outbox_snapshot(path: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for kind, table in (("enqueue", "conversation_memory_outbox"),
                            ("delete", "conversation_delete_outbox")):
            if table not in names:
                raise RuntimeError("MEMORY_OUTBOX_SCHEMA_MISSING")
            pending, oldest = db.execute(
                f"SELECT count(*), min(created_at) FROM {table} WHERE delivered_at IS NULL"
            ).fetchone()
            result[kind] = {"pending": pending,
                            "oldest_seconds": (max(0.0, (now - datetime.fromisoformat(oldest))
                                                   .total_seconds()) if oldest else None)}
        return result
    finally:
        db.close()


def postgres_outbox_snapshot(dsn: str, schema: str, *,
                             now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    with psycopg.connect(dsn, connect_timeout=3) as db:
        db.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        result = {}
        for kind, table in (("enqueue", "conversation_memory_outbox"),
                            ("delete", "conversation_delete_outbox")):
            pending, oldest = db.execute(
                sql.SQL("SELECT count(*), min(created_at) FROM {} "
                        "WHERE delivered_at IS NULL").format(sql.Identifier(table))
            ).fetchone()
            result[kind] = {"pending": pending,
                            "oldest_seconds": (max(0.0, (now - datetime.fromisoformat(oldest))
                                                   .total_seconds()) if oldest else None)}
        return result


def assess(*, api_ready: bool, postgres_ready: bool, outboxes: dict | None,
           max_pending: int = 100, max_age_seconds: float = 60.0) -> list[str]:
    alerts = []
    if not api_ready:
        alerts.append("api_unready")
    if not postgres_ready:
        alerts.append("postgres_unavailable")
    if outboxes is None:
        alerts.append("outbox_unavailable")
    else:
        for kind in ("enqueue", "delete"):
            row = outboxes[kind]
            if row["pending"] > max_pending:
                alerts.append(f"{kind}_backlog_count")
            if (row["oldest_seconds"] is not None
                    and row["oldest_seconds"] > max_age_seconds):
                alerts.append(f"{kind}_backlog_age")
    return alerts


def check_once(api_url: str, dsn: str, sqlite_path: Path,
               *, max_pending: int = 100, max_age_seconds: float = 60.0,
               conversation_backend: str = "sqlite", schema: str = "memory") -> dict:
    try:
        with urllib.request.urlopen(api_url, timeout=3) as response:
            api_ready = response.status == 200
    except Exception:
        api_ready = False
    try:
        with psycopg.connect(dsn, connect_timeout=3) as connection:
            postgres_ready = connection.execute("SELECT 1").fetchone()[0] == 1
    except Exception:
        postgres_ready = False
    try:
        outboxes = (postgres_outbox_snapshot(dsn, schema)
                    if conversation_backend == "postgres" else outbox_snapshot(sqlite_path))
    except Exception:
        outboxes = None
    alerts = assess(api_ready=api_ready, postgres_ready=postgres_ready,
                    outboxes=outboxes, max_pending=max_pending,
                    max_age_seconds=max_age_seconds)
    return {"alerts": alerts, "api_ready": api_ready,
            "postgres_ready": postgres_ready, "outboxes": outboxes}


def publish(event: dict, webhook_url: str) -> None:
    if not webhook_url.startswith(("http://", "https://")):
        raise ValueError("INVALID_MEMORY_MONITOR_WEBHOOK_URL")
    request = urllib.request.Request(
        webhook_url, data=json.dumps(event, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("MEMORY_MONITOR_WEBHOOK_FAILED")


def main() -> None:
    api_url = os.getenv("MEMORY_MONITOR_API_URL", "http://api:8000/readyz")
    dsn = os.getenv("MEMORY_DATABASE_URL", "")
    sqlite_path = Path(os.getenv("MEMORY_MONITOR_SQLITE_PATH",
                               "/app/data/demo_conversations.sqlite3"))
    if not dsn:
        raise ValueError("MEMORY_DATABASE_URL_REQUIRED")
    interval = float(os.getenv("MEMORY_MONITOR_INTERVAL_SECONDS", "5"))
    max_pending = int(os.getenv("MEMORY_MONITOR_MAX_PENDING", "100"))
    max_age = float(os.getenv("MEMORY_MONITOR_MAX_AGE_SECONDS", "60"))
    webhook_url = os.getenv("MEMORY_MONITOR_WEBHOOK_URL", "").strip()
    conversation_backend = os.getenv("CONVERSATION_BACKEND", "sqlite").strip().lower()
    schema = os.getenv("MEMORY_SCHEMA", "memory")
    if (not 1 <= interval <= 60 or max_pending < 0 or max_age < 0
            or conversation_backend not in ("sqlite", "postgres")):
        raise ValueError("INVALID_MEMORY_MONITOR_CONFIGURATION")
    previous = None
    while True:
        snapshot = check_once(api_url, dsn, sqlite_path,
                              max_pending=max_pending, max_age_seconds=max_age,
                              conversation_backend=conversation_backend, schema=schema)
        current = tuple(snapshot["alerts"])
        if current != previous:
            event = {"event": "MEMORY_ALERT" if current else "MEMORY_RECOVERED",
                     "at": datetime.now(timezone.utc).isoformat(),
                     "reasons": list(current),
                     "api_ready": snapshot["api_ready"],
                     "postgres_ready": snapshot["postgres_ready"],
                     "enqueue_pending": (snapshot["outboxes"] or {}).get("enqueue", {}).get("pending"),
                     "delete_pending": (snapshot["outboxes"] or {}).get("delete", {}).get("pending")}
            print(json.dumps(event, separators=(",", ":")), flush=True)
            if webhook_url:
                try:
                    publish(event, webhook_url)
                except Exception:
                    print(json.dumps({"event": "MEMORY_ALERT_DELIVERY_FAILED",
                                      "at": datetime.now(timezone.utc).isoformat()}),
                          flush=True)
                    time.sleep(interval)
                    continue
            previous = current
        time.sleep(interval)


if __name__ == "__main__":
    main()
