"""PostgreSQL document authority checks in an isolated imported lab schema."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.api.documents import DocumentRepository, OriginalVersionChanged
from app.api.file_access_questions import answer_file_access, parse_file_access_query
from app.api.pg_documents import PgDocumentRepository
from app.security.policy import Classification, ResourceAction, Subject, UserRole, Visibility
from eval.migrate_sqlite_documents import apply_snapshot, source_snapshot


MANAGER = Subject("manager", "lab-org", frozenset({"product"}),
                  Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
EMPLOYEE = Subject("employee", "lab-org", frozenset({"general"}), Classification.INTERNAL)
OUTSIDER = Subject("employee", "other-org", frozenset({"general"}), Classification.RESTRICTED)


class PgDocumentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
        if not cls.dsn or conninfo_to_dict(cls.dsn).get("dbname") != "agent_memory_lab":
            raise unittest.SkipTest("dedicated memory lab database required")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "documents.sqlite3"
        source = DocumentRepository(path, record_index_events=True)
        self.document_id = source.create_upload(
            MANAGER, title="试点计划", filename="plan.txt", file_type="TXT",
            area="项目空间", visibility=Visibility.ORGANIZATION,
            content=b"first source", extracted_text="第一版试点计划")
        self.assertTrue(source.publish(MANAGER, self.document_id))
        self.hidden_id = source.create_upload(
            MANAGER, title="产品计划", filename="product.txt", file_type="TXT",
            area="项目空间", visibility=Visibility.DEPARTMENT,
            content=b"department only", extracted_text="部门计划")
        self.assertTrue(source.publish(MANAGER, self.hidden_id))
        source.close()
        _, _, digest = source_snapshot(path)
        self.schema = "document_test_" + uuid4().hex[:12]
        apply_snapshot(path, self.dsn, schema=self.schema, expected_sha256=digest)
        self.first = PgDocumentRepository(self.dsn, schema=self.schema)
        self.second = PgDocumentRepository(self.dsn, schema=self.schema)

    def tearDown(self):
        self.first.close()
        self.second.close()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(self.schema)))
        self.temp.cleanup()

    def test_cross_instance_acl_original_and_epoch(self):
        self.assertEqual({item.document_id for item in self.second.list_visible(EMPLOYEE)},
                         {self.document_id})
        self.assertIsNone(self.second.get_visible(OUTSIDER, self.document_id))
        self.assertEqual(self.second.text_for(EMPLOYEE, self.document_id,
                                              ResourceAction.READ)[1], "第一版试点计划")
        self.assertEqual(self.second.original_for(EMPLOYEE, self.document_id),
                         ("plan.txt", "TXT", b"first source"))
        with self.assertRaises(OriginalVersionChanged):
            self.second.original_for(EMPLOYEE, self.document_id, "v0.9")
        epoch = self.first.policy_epoch()
        self.assertTrue(self.first.set_denied("lab-org", self.document_id, EMPLOYEE.user_id, True))
        self.assertEqual(self.second.policy_epoch(), epoch + 1)
        self.assertIsNone(self.second.get_visible(EMPLOYEE, self.document_id))
        self.assertIsNone(self.second.original_for(EMPLOYEE, self.document_id))
        self.assertEqual(self.second.list_visible(EMPLOYEE), [])
        event = self.second.connection.execute("""SELECT document_id FROM search_index_outbox
            ORDER BY event_id DESC LIMIT 1""").fetchone()
        self.assertEqual(event["document_id"], self.document_id)
        self.assertTrue(self.second.set_denied("lab-org", self.document_id, EMPLOYEE.user_id, False))
        self.assertIsNotNone(self.first.get_visible(EMPLOYEE, self.document_id))

    def test_concurrent_denies_do_not_lose_a_user(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(repository.set_denied, "lab-org", self.document_id,
                                      user_id, True)
                       for repository, user_id in ((self.first, "employee-a"),
                                                   (self.second, "employee-b"))]
            self.assertEqual([future.result() for future in futures], [True, True])
        row = self.first.connection.execute("""SELECT denied_user_ids FROM documents
            WHERE document_id = %s""", (self.document_id,)).fetchone()
        self.assertEqual(set(json.loads(row["denied_user_ids"])),
                         {"employee-a", "employee-b"})

    def test_failed_epoch_update_rolls_back_acl_change(self):
        self.first.connection.execute("DELETE FROM search_policy_epoch")
        with self.assertRaisesRegex(ValueError, "DOCUMENT_POLICY_EPOCH_MISSING"):
            self.first.set_denied("lab-org", self.document_id, EMPLOYEE.user_id, True)
        self.assertIsNotNone(self.second.get_visible(EMPLOYEE, self.document_id))

    def test_permission_answer_uses_current_shared_acl(self):
        query = parse_file_access_query("我能下载这份文件吗？")
        before = answer_file_access(query, EMPLOYEE, self.second, self.document_id)
        self.assertIn("可以下载", before.text)
        self.assertTrue(self.first.set_denied("lab-org", self.document_id, EMPLOYEE.user_id, True))
        self.assertIsNone(answer_file_access(query, EMPLOYEE, self.second, self.document_id))
        named = answer_file_access(parse_file_access_query("我能下载《试点计划》吗？"),
                                   EMPLOYEE, self.second)
        self.assertNotIn("试点计划", named.text)


if __name__ == "__main__":
    unittest.main()
