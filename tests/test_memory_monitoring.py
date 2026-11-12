"""Alert conditions are aggregate and fail closed on source or target failure."""

import sqlite3
import tempfile
import unittest
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.api.memory_monitoring import assess, outbox_snapshot, publish


class MemoryMonitoringTests(unittest.TestCase):
    def test_independent_failures_and_backlog_thresholds(self):
        healthy = {"enqueue": {"pending": 0, "oldest_seconds": None},
                   "delete": {"pending": 0, "oldest_seconds": None}}
        self.assertEqual(assess(api_ready=True, postgres_ready=True, outboxes=healthy), [])
        self.assertEqual(assess(api_ready=False, postgres_ready=False, outboxes=None),
                         ["api_unready", "postgres_unavailable", "outbox_unavailable"])
        backlog = {"enqueue": {"pending": 101, "oldest_seconds": 61},
                   "delete": {"pending": 1, "oldest_seconds": 61}}
        self.assertEqual(assess(api_ready=True, postgres_ready=True, outboxes=backlog),
                         ["enqueue_backlog_count", "enqueue_backlog_age",
                          "delete_backlog_age"])

    def test_sqlite_snapshot_contains_only_counts_and_age(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.sqlite3"
            now = datetime.now(timezone.utc)
            db = sqlite3.connect(path)
            for table in ("conversation_memory_outbox", "conversation_delete_outbox"):
                db.execute(f"CREATE TABLE {table} (created_at text, delivered_at text)")
            db.execute("INSERT INTO conversation_memory_outbox VALUES (?, NULL)",
                       ((now - timedelta(seconds=70)).isoformat(),))
            db.execute("INSERT INTO conversation_delete_outbox VALUES (?, NULL)",
                       ((now - timedelta(seconds=2)).isoformat(),))
            db.commit()
            db.close()
            result = outbox_snapshot(path, now=now)
            self.assertEqual(result["enqueue"]["pending"], 1)
            self.assertGreaterEqual(result["enqueue"]["oldest_seconds"], 70)
            self.assertEqual(result["delete"]["pending"], 1)
            self.assertNotIn("owner_id", str(result))

    def test_alert_webhook_delivers_only_aggregate_event(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(
                    int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            event = {"event": "MEMORY_ALERT", "reasons": ["postgres_unavailable"],
                     "enqueue_pending": 0, "delete_pending": 0}
            publish(event, f"http://127.0.0.1:{server.server_port}/alert")
            self.assertEqual(received, [event])
            with self.assertRaisesRegex(ValueError, "INVALID_MEMORY_MONITOR_WEBHOOK_URL"):
                publish(event, "file:///tmp/alert")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
