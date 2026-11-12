"""Local, append-only audit metadata for development workflows."""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditRepository:
    def __init__(self, path: str | Path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                org_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                trace_id TEXT NOT NULL
            )""")
            self._db.execute("CREATE INDEX IF NOT EXISTS audit_events_org_id ON audit_events(org_id, id DESC)")

    def record(self, *, org_id: str, actor_id: str, action: str, resource_type: str,
               resource_id: str, outcome: str, trace_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO audit_events
                (occurred_at, org_id, actor_id, action, resource_type, resource_id, outcome, trace_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    org_id, actor_id, action, resource_type, resource_id, outcome, trace_id,
                ))

    def list_for_org(self, org_id: str, *, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""SELECT * FROM audit_events WHERE org_id = ?
                ORDER BY id DESC LIMIT ?""", (org_id, min(max(limit, 1), 100))).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._db.close()
