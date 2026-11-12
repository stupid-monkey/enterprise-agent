"""Stopped document catalog import into an isolated PostgreSQL lab schema."""

import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

from app.api.documents import DocumentRepository
from app.security.policy import Classification, Subject, UserRole, Visibility
from eval.migrate_sqlite_documents import apply_snapshot, source_snapshot


MANAGER = Subject("manager", "synthetic-org", frozenset({"product"}),
                  Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)


class PgDocumentMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
        if not cls.dsn or psycopg.conninfo.conninfo_to_dict(cls.dsn).get("dbname") != "agent_memory_lab":
            raise unittest.SkipTest("dedicated memory lab database required")

    def test_catalog_and_original_bytes_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "documents.sqlite3"
            catalog = DocumentRepository(path, record_index_events=True)
            document_id = catalog.create_upload(
                MANAGER, title="项目计划", filename="plan.txt", file_type="TXT",
                area="项目资料", visibility=Visibility.DEPARTMENT,
                content=b"original bytes", extracted_text="第一版项目计划")
            self.assertTrue(catalog.publish(MANAGER, document_id))
            self.assertEqual(catalog.create_revision(
                MANAGER, document_id, filename="plan-v2.txt", file_type="TXT",
                content=b"revised bytes", extracted_text="第二版项目计划"), "v1.1")
            self.assertEqual(catalog.publish_revision(MANAGER, document_id), "v1.1")
            self.assertTrue(catalog.set_denied(MANAGER.org_id, document_id, "employee", True))
            second_id = catalog.create_upload(
                MANAGER, title="差旅制度", filename="travel.txt", file_type="TXT",
                area="制度规范", visibility=Visibility.ORGANIZATION,
                content=b"travel bytes", extracted_text="差旅报销先经部门审批")
            self.assertTrue(catalog.publish(MANAGER, second_id))
            catalog.close()

            _, source_summary, source_hash = source_snapshot(path)
            self.assertEqual(source_summary["documents"]["count"], 2)
            self.assertEqual(source_summary["document_revisions"]["count"], 2)
            schema = "document_test_" + uuid4().hex[:12]
            try:
                with self.assertRaisesRegex(ValueError, "DOCUMENT_SNAPSHOT_HASH_MISMATCH"):
                    apply_snapshot(path, self.dsn, schema=schema, expected_sha256="0" * 64)
                result = apply_snapshot(path, self.dsn, schema=schema,
                                        expected_sha256=source_hash)
                self.assertTrue(result["verified"])
                self.assertEqual(result["tables"], source_summary)
                self.assertNotIn("revised bytes", str(result))
                with psycopg.connect(self.dsn) as connection:
                    current = connection.execute(sql.SQL(
                        "SELECT content FROM {}.document_files WHERE document_id = %s").format(
                            sql.Identifier(schema)), (document_id,)).fetchone()
                    self.assertEqual(current[0], b"revised bytes")
                    previous = connection.execute(sql.SQL(
                        "SELECT content FROM {}.document_revisions WHERE document_id = %s "
                        "AND version = 'v1.0'").format(sql.Identifier(schema)),
                        (document_id,)).fetchone()
                    self.assertEqual(previous[0], b"original bytes")
                with self.assertRaisesRegex(ValueError, "DOCUMENT_TARGET_SCHEMA_EXISTS"):
                    apply_snapshot(path, self.dsn, schema=schema,
                                   expected_sha256=source_hash)
            finally:
                with psycopg.connect(self.dsn, autocommit=True) as connection:
                    connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema)))


class DocumentSnapshotTests(unittest.TestCase):
    def test_dry_run_does_not_need_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "documents.sqlite3"
            catalog = DocumentRepository(path)
            catalog.close()
            _, summary, source_hash = source_snapshot(path)
            self.assertEqual(len(summary), 9)
            self.assertEqual(summary["search_index_outbox"]["count"], 0)
            self.assertEqual(len(source_hash), 64)
            journal = Path(str(path) + "-journal")
            journal.touch()
            with self.assertRaisesRegex(ValueError, "DOCUMENT_SNAPSHOT_HAS_ACTIVE_JOURNAL"):
                source_snapshot(path)


if __name__ == "__main__":
    unittest.main()
