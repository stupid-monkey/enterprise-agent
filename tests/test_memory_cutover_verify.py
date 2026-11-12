import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from psycopg import sql

from app.api.pg_user_memories import PgUserMemoryRepository
from app.api.user_memories import UserMemoryRepository
from app.security.policy import Classification, Subject
from eval.migrate_sqlite_memories import import_snapshot, read_snapshot
from eval.verify_memory_cutover import verify_snapshot


ALICE = Subject("alice", "org-a", frozenset(), Classification.INTERNAL)


class MemoryCutoverVerifyTests(unittest.TestCase):
    def test_wrong_hash_rejected_before_target_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.sqlite3"
            repository = UserMemoryRepository(path)
            repository.upsert(ALICE, "role", "设计师")
            repository.close()
            previous_directory = Path.cwd()
            try:
                os.chdir(path.parent)
                relative_hash = read_snapshot(Path(path.name))["source_sha256"]
            finally:
                os.chdir(previous_directory)
            self.assertEqual(relative_hash, read_snapshot(path)["source_sha256"])
            with self.assertRaisesRegex(ValueError, "MEMORY_SNAPSHOT_HASH_MISMATCH"):
                verify_snapshot(path, "0" * 64, "postgresql://invalid.invalid/db")

    @unittest.skipUnless(os.getenv("MEMORY_TEST_DATABASE_URL"),
                         "requires dedicated synthetic PostgreSQL test database")
    def test_lab_snapshot_match_then_detects_target_drift_without_exposing_values(self):
        dsn = os.environ["MEMORY_TEST_DATABASE_URL"]
        schema = "memory_test_" + uuid4().hex[:12]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.sqlite3"
            sqlite_memory = UserMemoryRepository(path)
            sqlite_memory.upsert(ALICE, "role", "设计师")
            sqlite_memory.upsert(ALICE, "task", "负责知识库迁移")
            sqlite_memory.set_auto_suggest(ALICE, True)
            sqlite_memory.delete(ALICE, "task")
            sqlite_memory.close()
            snapshot = read_snapshot(path)
            import_snapshot(snapshot, dsn, schema=schema)
            pg = PgUserMemoryRepository(dsn, schema=schema)
            try:
                wrong_database = dsn.rsplit("/", 1)[0] + "/postgres"
                with self.assertRaisesRegex(RuntimeError, "MEMORY_VERIFY_LAB_DATABASE_REQUIRED"):
                    verify_snapshot(path, snapshot["source_sha256"], wrong_database,
                                    schema=schema)
                report = verify_snapshot(path, snapshot["source_sha256"], dsn, schema=schema)
                self.assertTrue(report["verified"])
                self.assertTrue(all(check["match"] for check in report["checks"].values()))
                self.assertNotIn("设计师", str(report))
                self.assertNotIn("知识库迁移", str(report))
                pg.connection.execute("""UPDATE memory_fact SET value = '不同岗位'
                    WHERE org_id = 'org-a' AND owner_id = 'alice' AND kind = 'role'""")
                changed = verify_snapshot(path, snapshot["source_sha256"], dsn, schema=schema)
                self.assertFalse(changed["verified"])
                self.assertFalse(changed["checks"]["facts"]["match"])
                self.assertTrue(changed["checks"]["settings"]["match"])
                self.assertTrue(changed["checks"]["suppressions"]["match"])
                self.assertNotIn("不同岗位", str(changed))
                self.assertEqual(pg.list_for(ALICE)[0]["value"], "不同岗位")
                self.assertEqual(read_snapshot(path)["source_sha256"], snapshot["source_sha256"])
            finally:
                pg.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema)))
                pg.close()


if __name__ == "__main__":
    unittest.main()
