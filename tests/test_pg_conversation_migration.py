"""Offline SQLite conversation import preserves IDs and outbox state."""

import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.api.conversations import ConversationRepository
from app.api.pg_conversations import PgConversationRepository
from app.security.policy import Classification, Subject
from eval.migrate_sqlite_conversations import apply_snapshot, source_snapshot


class PgConversationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
        if not cls.dsn or conninfo_to_dict(cls.dsn).get("dbname") != "agent_memory_lab":
            raise unittest.SkipTest("dedicated memory lab database required")

    def test_import_and_id_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.sqlite3"
            subject = Subject("alice", "synthetic-org", frozenset(),
                              Classification.INTERNAL)
            sqlite = ConversationRepository(path, enable_delete_outbox=True)
            first_id = sqlite.append(subject, None, "我目前是风控分析师。",
                                     "general", {"status": "ordinary"},
                                     memory_include_ordinary=True)
            sqlite.append(subject, first_id, "第二轮", "general", {},
                          memory_include_ordinary=False)
            deleted_id = sqlite.append(subject, None, "删除我", "general", {})
            sqlite.delete(subject, deleted_id)
            sqlite.close()
            _, source_summary, source_hash = source_snapshot(path)
            schema = "conversation_test_" + uuid4().hex[:12]
            try:
                result = apply_snapshot(path, self.dsn, schema=schema,
                                        expected_sha256=source_hash)
                self.assertTrue(result["verified"])
                self.assertEqual(result["tables"], source_summary)
                pg = PgConversationRepository(self.dsn, schema=schema)
                try:
                    self.assertEqual(len(pg.list_for(subject)), 1)
                    self.assertEqual(pg.pending_memory_event_count(), 1)
                    self.assertEqual(pg.pending_delete_count(), 1)
                    self.assertEqual(pg.claim_memory_event()["conversation_id"], first_id)
                    pg.append(subject, first_id, "第三轮", "general", {})
                    ids = [row["id"] for row in pg.connection.execute(
                        "SELECT id FROM conversation_turns ORDER BY id")]
                    self.assertEqual(len(ids), len(set(ids)))
                    self.assertEqual(ids, sorted(ids))
                    with self.assertRaisesRegex(ValueError, "CONVERSATION_TARGET_NOT_EMPTY"):
                        apply_snapshot(path, self.dsn, schema=schema,
                                       expected_sha256=source_hash)
                finally:
                    pg.close()
            finally:
                with psycopg.connect(self.dsn, autocommit=True) as connection:
                    connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE")
                                       .format(sql.Identifier(schema)))

    def test_wrong_hash_rejected_before_target_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.sqlite3"
            sqlite = ConversationRepository(path, enable_delete_outbox=True)
            sqlite.close()
            schema = "conversation_test_" + uuid4().hex[:12]
            with self.assertRaisesRegex(ValueError, "CONVERSATION_SNAPSHOT_HASH_MISMATCH"):
                apply_snapshot(path, self.dsn, schema=schema,
                               expected_sha256="0" * 64)
            with psycopg.connect(self.dsn, autocommit=True) as connection:
                row = connection.execute("SELECT to_regnamespace(%s)",
                                         (schema,)).fetchone()
                self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
