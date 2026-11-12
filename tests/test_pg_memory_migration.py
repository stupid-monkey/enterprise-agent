import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from threading import Event
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock
from uuid import uuid4

from psycopg import sql
import psycopg
from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.pg_user_memories import (PgUserMemoryRepository, QUEUE_SCHEMA,
                                      QUEUE_V2, QUEUE_V3, QUEUE_V4, SCHEMA)
from app.api.main import (MemoryResponse, MemorySuggestionFailureResponse,
                          MemorySuggestionResponse, create_app)
from app.api.conversations import ConversationRepository
from app.api.user_memories import UserMemoryRepository
from app.security.policy import Classification, Subject
from eval.migrate_sqlite_memories import (digest, import_snapshot, main,
                                          read_conversation_snapshot, read_snapshot)
from eval.verify_memory_cutover import verify_snapshot


ALICE = Subject("alice", "org-a", frozenset(), Classification.INTERNAL)
BOB = Subject("bob", "org-a", frozenset(), Classification.INTERNAL)
OTHER_ORG = Subject("alice", "org-b", frozenset(), Classification.INTERNAL)


class PgMemoryMigrationTests(unittest.TestCase):
    def create_snapshot(self, path: Path) -> dict:
        repository = UserMemoryRepository(path)
        try:
            repository.set_auto_suggest(ALICE, True)
            repository.upsert(ALICE, "role", "设计师")
            repository.upsert_auto(ALICE, "task", "长期负责知识库迁移",
                                   "conversation-1", "我长期负责知识库迁移")
            repository.delete(BOB, "role")
        finally:
            repository.close()


        return read_snapshot(path)

    def test_offline_snapshot_contains_only_current_fact_and_no_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            snapshot = self.create_snapshot(path)
            self.assertEqual(len(snapshot["facts"]), 2)
            self.assertEqual(len(snapshot["settings"]), 1)
            self.assertEqual(len(snapshot["suppressions"]), 0)
            self.assertNotIn("question", str(snapshot))
            self.assertEqual(len(digest(snapshot["facts"])), 64)
            output = StringIO()
            with mock.patch.object(sys, "argv", ["migrate", "--snapshot", str(path)]):
                with redirect_stdout(output):
                    main()
            self.assertNotIn("设计师", output.getvalue())
            self.assertNotIn("知识库迁移", output.getvalue())
            self.assertIn("source_sha256", output.getvalue())

    def test_snapshot_with_wal_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            self.create_snapshot(path)
            wal = path.with_name(path.name + "-wal")
            wal.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "SQLITE_SNAPSHOT_HAS_WAL"):
                read_snapshot(path)

    def test_expired_fact_is_omitted_and_apply_requires_matching_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            self.create_snapshot(path)
            db = sqlite3.connect(path)
            try:
                with db:
                    db.execute("""UPDATE user_memories SET expires_at =
                        '2000-01-01T00:00:00+00:00' WHERE kind = 'task'""")
            finally:
                db.close()
            self.assertEqual(len(read_snapshot(path)["facts"]), 1)
            with mock.patch.object(sys, "argv", ["migrate", "--snapshot", str(path),
                                                "--apply", "--expected-sha256", "wrong"]):
                with self.assertRaisesRegex(ValueError, "MEMORY_SNAPSHOT_HASH_MISMATCH"):
                    main()

    @unittest.skipUnless(os.getenv("MEMORY_TEST_DATABASE_URL"),
                         "requires dedicated synthetic PostgreSQL test database")
    def test_live_import_rejects_second_import_and_scopes_reads(self):
        dsn = os.environ["MEMORY_TEST_DATABASE_URL"]
        schema = "memory_test_" + uuid4().hex[:12]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            snapshot = self.create_snapshot(path)
            output = StringIO()
            with mock.patch.dict(os.environ, {"MEMORY_DATABASE_URL": dsn}):
                with mock.patch.object(sys, "argv", ["migrate", "--snapshot", str(path),
                                                    "--schema", schema, "--apply",
                                                    "--expected-sha256", snapshot["source_sha256"]]):
                    with redirect_stdout(output):
                        main()
            self.assertIn('"verified": true', output.getvalue())
            self.assertNotIn("设计师", output.getvalue())
            with self.assertRaisesRegex(RuntimeError, "POSTGRES_MEMORY_DESTINATION_NOT_EMPTY"):
                import_snapshot(snapshot, dsn, schema=schema)
            wrong_database = dsn.rsplit("/", 1)[0] + "/postgres"
            with self.assertRaisesRegex(RuntimeError, "MEMORY_MIGRATION_LAB_DATABASE_REQUIRED"):
                import_snapshot(snapshot, wrong_database, schema="memory_test_rejected")
        repository = PgUserMemoryRepository(dsn, schema=schema)
        try:
            self.assertEqual(len(repository.list_for(ALICE)), 2)
            self.assertEqual(repository.list_for(BOB), [])
            self.assertEqual(repository.list_for(OTHER_ORG), [])
            self.assertEqual(repository.discard_conversation_auto(BOB, "conversation-1"), 0)
            self.assertEqual(repository.discard_conversation_auto(ALICE, "conversation-1"), 1)
            self.assertEqual(repository.list_for(ALICE)[0]["kind"], "role")
            edited = repository.upsert(ALICE, "role", "产品经理")
            self.assertEqual(edited["value"], "产品经理")
            self.assertEqual(edited["version"], 2)
            repository.delete(OTHER_ORG, "role")
            self.assertEqual(repository.list_for(ALICE)[0]["value"], "产品经理")
        finally:
            repository.close()


@unittest.skipUnless(os.getenv("MEMORY_PRODUCT_TEST_DATABASE_URL"),
                     "requires isolated agent_memory database for cutover rehearsal")
class ProductMemoryCutoverTests(unittest.TestCase):
    def test_offline_import_verifies_sources_candidates_and_manifest(self):
        dsn = os.environ["MEMORY_PRODUCT_TEST_DATABASE_URL"]
        schema = "memory_test_" + uuid4().hex[:12]
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory.sqlite3"
            conversation_path = Path(directory) / "conversations.sqlite3"
            conversation = ConversationRepository(conversation_path)
            source = conversation.append(ALICE, None, "我是设计师", "general",
                                         {"paragraphs": ["ok"], "citations": []})
            conversation.close()
            memory = UserMemoryRepository(memory_path)
            memory.set_auto_suggest(ALICE, True)
            memory.upsert(ALICE, "answer_preference", "简短回答")
            memory.upsert_auto(ALICE, "role", "我是设计师", source, "我是设计师")
            memory.propose(ALICE, "task", "负责知识库", source, "我负责知识库")
            memory.close()
            snapshot = read_snapshot(memory_path)
            sources = read_conversation_snapshot(conversation_path)
            try:
                self.assertTrue(import_snapshot(
                    snapshot, dsn, schema=schema, target_database="agent_memory",
                    conversations=sources)["verified"])
                report = verify_snapshot(
                    memory_path, snapshot["source_sha256"], dsn, schema=schema,
                    target_database="agent_memory",
                    conversation_snapshot_path=conversation_path,
                    expected_conversation_sha256=sources["source_sha256"])
                self.assertTrue(report["verified"])
                pg = PgUserMemoryRepository(dsn, schema=schema)
                try:
                    pg.verify_import_manifest(snapshot["source_sha256"],
                                              sources["source_sha256"])
                    self.assertEqual(len(pg.list_for(ALICE)), 2)
                    self.assertEqual(len(pg.list_suggestions(ALICE)), 1)
                    self.assertTrue(pg.register_source(ALICE, source))
                    with self.assertRaisesRegex(RuntimeError,
                                                "MEMORY_IMPORT_MANIFEST_MISMATCH"):
                        pg.verify_import_manifest("0" * 64,
                                                  sources["source_sha256"])
                finally:
                    pg.close()
                with self.assertRaisesRegex(RuntimeError,
                                            "POSTGRES_MEMORY_DESTINATION_NOT_EMPTY"):
                    import_snapshot(snapshot, dsn, schema=schema,
                                    target_database="agent_memory", conversations=sources)
                auth = LocalAuth([LocalAccount("alice", hash_password("password"), ALICE)])
                active_conversations = ConversationRepository(
                    conversation_path, enable_delete_outbox=True)
                env = {"MEMORY_BACKEND": "postgres", "MEMORY_DATABASE_URL": dsn,
                       "MEMORY_SCHEMA": schema,
                       "MEMORY_CUTOVER_MEMORY_SHA256": snapshot["source_sha256"],
                       "MEMORY_CUTOVER_CONVERSATION_SHA256": sources["source_sha256"]}
                try:
                    with mock.patch.dict(os.environ, env):
                        application = create_app(auth=auth, conversations=active_conversations)
                    with TestClient(application) as client:
                        token = client.post("/api/v1/auth/login", json={
                            "username": "alice", "password": "password"}).json()["access_token"]
                        headers = {"Authorization": f"Bearer {token}"}
                        self.assertEqual(client.get("/readyz").status_code, 200)
                        self.assertEqual(len(client.get(
                            "/api/v1/me/memories", headers=headers).json()), 2)
                    application.state.memories.close()
                    with mock.patch.dict(os.environ, {**env,
                            "MEMORY_CUTOVER_MEMORY_SHA256": "0" * 64}):
                        with self.assertRaisesRegex(RuntimeError,
                                                    "MEMORY_IMPORT_MANIFEST_MISMATCH"):
                            create_app(auth=auth, conversations=active_conversations)
                finally:
                    active_conversations.close()
            finally:
                cleanup = psycopg.connect(dsn, autocommit=True)
                cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)))
                cleanup.close()


@unittest.skipUnless(os.getenv("MEMORY_TEST_DATABASE_URL"),
                     "requires dedicated synthetic PostgreSQL test database")
class PgMemoryQueueTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["MEMORY_TEST_DATABASE_URL"]
        self.schema = "memory_test_" + uuid4().hex[:12]
        self.first = PgUserMemoryRepository(self.dsn, schema=self.schema)
        self.first.create_schema()
        self.second = PgUserMemoryRepository(self.dsn, schema=self.schema)
        self.first.set_auto_suggest(ALICE, True)

    def _enqueue(self, subject, question, conversation_id, context=()):
        self.first.register_source(subject, conversation_id)
        return self.first.enqueue_suggestion(subject, question, conversation_id, context)

    def test_candidate_review_is_owner_scoped_and_manual_confirmation_wins(self):
        question = "我是设计师，正在负责知识库迁移"
        job = self._enqueue(ALICE, question, "review-source")
        claim = self.first.claim_suggestion_job()
        lease = (job, claim["lease_token"])
        self.first.propose(BOB, "role", "我是设计师", "review-source", question,
                           job_lease=lease)
        self.first.propose(ALICE, "role", "我是设计师", "wrong-source", question,
                           job_lease=lease)
        self.first.propose(ALICE, "role", "我是设计师", "review-source", "伪造原话",
                           job_lease=lease)
        self.assertEqual(self.first.list_suggestions(ALICE), [])
        self.first.propose(ALICE, "role", "我是设计师", "review-source", question,
                           job_lease=lease)
        self.first.propose(ALICE, "role", "我是设计师", "review-source", question,
                           job_lease=lease)
        rows = self.first.list_suggestions(ALICE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.first.list_suggestions(BOB), [])
        self.assertFalse(self.first.reject_suggestion(BOB, rows[0]["id"]))
        self.assertIsNone(self.first.accept_suggestion(BOB, rows[0]["id"], "其他岗位"))
        accepted = self.first.accept_suggestion(ALICE, rows[0]["id"], "设计负责人")
        self.assertEqual(accepted["value"], "设计负责人")
        self.assertEqual(accepted["origin"], "manual")
        self.assertEqual(self.first.list_suggestions(ALICE), [])
        self.assertIsNone(self.first.accept_suggestion(ALICE, rows[0]["id"], "重复确认"))
        self.assertIsNone(self.second.apply_auto_fact(job, claim["lease_token"],
                                                       "role", "我是设计师"))
        self.assertEqual(self.first.list_for(ALICE)[0]["value"], "设计负责人")

    def test_rejection_and_source_deletion_prevent_candidate_reappearance(self):
        question = "我是分析师，负责月度报表"
        job = self._enqueue(ALICE, question, "reject-source")
        claim = self.first.claim_suggestion_job()
        lease = (job, claim["lease_token"])
        self.first.propose(ALICE, "role", "我是分析师", "reject-source", question,
                           job_lease=lease)
        suggestion_id = self.first.list_suggestions(ALICE)[0]["id"]
        self.assertTrue(self.first.reject_suggestion(ALICE, suggestion_id))
        self.assertFalse(self.first.reject_suggestion(ALICE, suggestion_id))
        self.first.propose(ALICE, "role", "我是分析师", "reject-source", question,
                           job_lease=lease)
        self.assertEqual(self.first.list_suggestions(ALICE), [])
        self.first.propose(ALICE, "task", "负责月度报表", "reject-source", question,
                           job_lease=lease)
        self.assertEqual(len(self.first.list_suggestions(ALICE)), 1)
        self.assertTrue(self.first.delete_source(ALICE, "reject-source"))
        self.assertEqual(self.first.list_suggestions(ALICE), [])
        self.first.propose(ALICE, "task", "负责月度报表", "reject-source", question,
                           job_lease=lease)
        self.assertEqual(self.first.list_suggestions(ALICE), [])

    def test_closed_setting_and_stale_lease_block_candidate(self):
        question = "我是项目经理"
        job = self._enqueue(ALICE, question, "closed-source")
        claim = self.first.claim_suggestion_job()
        lease = (job, claim["lease_token"])
        self.first.propose(ALICE, "role", question, "closed-source", question,
                           job_lease=lease)
        self.assertEqual(len(self.first.list_suggestions(ALICE)), 1)
        self.first.set_auto_suggest(ALICE, False)
        self.assertEqual(self.first.list_suggestions(ALICE), [])
        self.first.propose(ALICE, "role", question, "closed-source", question,
                           job_lease=lease)
        self.assertEqual(self.first.list_suggestions(ALICE), [])
        self.first.set_auto_suggest(ALICE, True)
        self.first.propose(ALICE, "role", question, "closed-source", question,
                           job_lease=lease)
        self.assertEqual(self.first.list_suggestions(ALICE), [])

    def test_api_rows_serialize_dates_for_memory_endpoints(self):
        saved = self.first.upsert(ALICE, "role", "设计师")
        self.assertIsInstance(saved["created_at"], str)
        self.assertEqual(MemoryResponse.model_validate(saved).value, "设计师")
        self.assertEqual(MemoryResponse.model_validate(
            self.first.list_for(ALICE)[0]).value, "设计师")
        question = "我负责知识库迁移"
        job = self._enqueue(ALICE, question, "date-source")
        claim = self.first.claim_suggestion_job()
        self.first.propose(ALICE, "task", question, "date-source", question,
                           job_lease=(job, claim["lease_token"]))
        suggestion = self.first.list_suggestions(ALICE)[0]
        self.assertIsInstance(suggestion["created_at"], str)
        self.assertEqual(MemorySuggestionResponse.model_validate(suggestion).value,
                         question)
        self.assertTrue(self.first.fail_suggestion_job(
            job, claim["lease_token"], "model_invalid", retryable=False))
        failure = self.first.list_suggestion_failures(ALICE)[0]
        self.assertIsInstance(failure["failed_at"], str)
        self.assertEqual(MemorySuggestionFailureResponse.model_validate(
            failure).reason_code, "model_invalid")

    def test_same_session_retraction_requires_bound_context_and_valid_lease(self):
        old_text = "我长期负责数据平台迁移项目"
        self._save_auto("task", "长期负责数据平台迁移项目", old_text, "same-source")
        latest = "这个项目已经结束了"
        job = self._enqueue(ALICE, latest, "same-source", (old_text,))
        claim = self.first.claim_suggestion_job()
        lease = (job, claim["lease_token"])
        self.assertFalse(self.first.forget_auto_from_context(
            BOB, "task", "same-source", (old_text,), job_lease=lease))
        self.assertFalse(self.first.forget_auto_from_context(
            ALICE, "task", "same-source", ("伪造上下文",), job_lease=lease))
        self.assertFalse(self.first.forget_auto_from_context(
            ALICE, "task", "same-source", (old_text,),
            job_lease=(job, "invalid-token")))
        self.assertTrue(self.first.forget_auto_from_context(
            ALICE, "task", "same-source", (old_text,), job_lease=lease))
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertFalse(self.first.forget_auto_from_context(
            ALICE, "task", "same-source", (old_text,), job_lease=lease))

    def tearDown(self):
        self.second.close()
        self.first.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(self.schema)))
        self.first.close()

    def test_product_worker_shapes_keep_lease_source_and_owner_gates(self):
        self.assertTrue(self.first.auto_suggest_enabled(ALICE))
        self.assertFalse(self.first.auto_suggest_enabled(BOB))
        self.assertIsNone(self.first.next_suggestion_lease_delay())
        job_id = self._enqueue(ALICE, "我是设计师", "worker-source", ("我在做设计",))
        self.assertIsNotNone(job_id)
        self.assertEqual(self.first.suggestion_job_count(), 1)
        self.assertGreaterEqual(self.first.next_suggestion_lease_delay(), 0)
        claim = self.first.claim_suggestion_job()
        self.assertEqual(claim["context"], ("我在做设计",))
        self.assertNotIn("context_json", claim)
        self.assertIsNone(self.first.upsert_auto(
            ALICE, "role", "我是设计师", "worker-source", "我是设计师"))
        self.assertIsNone(self.first.upsert_auto(
            BOB, "role", "我是设计师", "worker-source", "我是设计师",
            job_lease=(job_id, claim["lease_token"])))
        self.assertIsNone(self.first.upsert_auto(
            ALICE, "role", "我是设计师", "wrong-source", "我是设计师",
            job_lease=(job_id, claim["lease_token"])))
        self.assertIsNone(self.first.upsert_auto(
            ALICE, "role", "我是设计师", "worker-source", "我是设计师",
            job_lease=(job_id, "wrong-token")))
        result = self.first.upsert_auto(
            ALICE, "role", "我是设计师", "worker-source", "我是设计师",
            job_lease=(job_id, claim["lease_token"]))
        self.assertEqual((result["origin"], result["source_conversation_id"]),
                         ("auto", "worker-source"))
        self.assertIsNotNone(result["updated_at"])
        self.first.discard_conversation_suggestions(ALICE, "worker-source")
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertEqual(self.first.suggestion_job_count(), 0)
        self.assertIsNone(self.first.next_suggestion_lease_delay())
        self.assertFalse(self.first.register_source(ALICE, "worker-source"))

    def test_product_retraction_checks_owner_source_and_snapshot(self):
        old = self._save_auto("task", "长期负责门店巡检系统上线",
                              "我长期负责门店巡检系统上线", "old-source")
        latest = "门店巡检系统上线已经移交同事"
        job_id = self._enqueue(ALICE, latest, "new-source")
        claim = self.first.claim_suggestion_job()
        lease = (job_id, claim["lease_token"])
        self.assertFalse(self.first.forget_auto_task_cross_session(
            BOB, old, "new-source", latest, "已经移交", job_lease=lease))
        self.assertFalse(self.first.forget_auto_task_cross_session(
            ALICE, old, "wrong-source", latest, "已经移交", job_lease=lease))
        self.assertFalse(self.first.forget_auto_task_cross_session(
            ALICE, old, "new-source", latest, "已经移交",
            job_lease=(job_id, "wrong-token")))
        self.assertEqual(self.first.list_for(ALICE)[0]["value"], old["value"])
        self.assertTrue(self.first.forget_auto_task_cross_session(
            ALICE, old, "new-source", latest, "已经移交", job_lease=lease))
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertFalse(self.first.forget_auto_task_cross_session(
            ALICE, old, "new-source", latest, "已经移交", job_lease=lease))

    def test_default_setting_is_persisted_and_explicit_opt_out_wins(self):
        default_on = PgUserMemoryRepository(self.dsn, schema=self.schema,
                                            default_auto_suggest=True)
        try:
            self.assertTrue(default_on.auto_suggest_enabled(BOB))
            self.assertTrue(default_on.register_source(BOB, "bob-source"))
            self.assertIsNotNone(default_on.enqueue_suggestion(
                BOB, "我是分析师", "bob-source"))
            self.assertEqual(default_on.suggestion_job_count(BOB), 1)
            default_on.set_auto_suggest(BOB, False)
            self.assertFalse(default_on.auto_suggest_enabled(BOB))
            self.assertEqual(default_on.suggestion_job_count(BOB), 0)
            self.assertFalse(self.second.auto_suggest_enabled(BOB))
            self.assertIsNone(default_on.enqueue_suggestion(BOB, "我是分析师", "bob-source"))
        finally:
            default_on.close()

    def test_claim_retry_fencing_and_max_attempts(self):
        job_id = self._enqueue(ALICE, "我负责季度项目汇报", "c1")
        self.assertIsNotNone(job_id)
        claim = self.first.claim_suggestion_job()
        self.assertEqual(claim["id"], job_id)
        self.assertIsNone(self.second.claim_suggestion_job())
        token = claim["lease_token"]
        self.assertIsNone(self.first.apply_auto_fact(job_id, "wrong", "task", "我负责季度项目汇报"))
        self.assertIsNone(self.first.apply_auto_fact(job_id, token, "task", "杜撰事实"))
        self.assertEqual(self.first.apply_auto_fact(job_id, token, "task", "我负责季度项目汇报")["origin"], "auto")
        self.assertIsNone(self.first.apply_auto_fact(job_id, token, "task", "我负责季度项目汇报"))
        self.assertTrue(self.first.fail_suggestion_job(job_id, token, "model_timeout", retryable=True))
        self.assertFalse(self.first.finish_suggestion_job(job_id, token))
        self.assertIsNone(self.second.claim_suggestion_job())
        self.first.connection.execute("UPDATE memory_job SET available_at = now() - interval '1 second' WHERE id = %s", (job_id,))
        next_claim = self.second.claim_suggestion_job()
        self.assertEqual(next_claim["attempts"], 2)
        self.assertIsNone(self.first.apply_auto_fact(job_id, token, "role", "我负责"))
        self.assertTrue(self.second.fail_suggestion_job(job_id, next_claim["lease_token"], "model_timeout", retryable=True))
        self.first.connection.execute("UPDATE memory_job SET available_at = now() - interval '1 second' WHERE id = %s", (job_id,))
        last = self.first.claim_suggestion_job()
        self.assertEqual(last["attempts"], 3)
        self.first.connection.execute("UPDATE memory_job SET lease_until = now() - interval '1 second' WHERE id = %s", (job_id,))
        self.assertIsNone(self.first.apply_auto_fact(job_id, last["lease_token"], "role", "我负责"))
        self.assertFalse(self.first.finish_suggestion_job(job_id, last["lease_token"]))
        self.second.recover_suggestion_jobs()
        self.assertEqual(self.first.suggestion_job_count(ALICE), 0)
        self.assertEqual(self.first.list_suggestion_failures(ALICE)[0]["reason_code"], "lease_exhausted")
        self.assertEqual(self.first.list_suggestion_failures(BOB), [])

    def test_two_workers_claim_distinct_jobs_concurrently(self):
        ids = {self._enqueue(ALICE, "我负责季度项目汇报", "c1"),
               self._enqueue(ALICE, "我是设计师", "c2")}
        barrier = Barrier(2)

        def claim(repository):
            barrier.wait()
            return repository.claim_suggestion_job()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(claim, self.first)
            second = pool.submit(claim, self.second)
            claims = (first.result(timeout=10), second.result(timeout=10))
        self.assertEqual({item["id"] for item in claims}, ids)
        self.assertEqual(len({item["lease_token"] for item in claims}), 2)

    def test_scope_manual_override_disable_and_source_delete(self):
        self.assertIsNone(self._enqueue(BOB, "我负责新项目", "c1"))
        self.first.set_auto_suggest(OTHER_ORG, True)
        other_job = self._enqueue(OTHER_ORG, "我负责其他组织项目", "c1")
        job = self._enqueue(ALICE, "我是设计师", "c1")
        claim = self.first.claim_suggestion_job()
        second_claim = self.second.claim_suggestion_job()
        alice = next(item for item in (claim, second_claim) if item["org_id"] == ALICE.org_id)
        other = next(item for item in (claim, second_claim) if item["org_id"] == OTHER_ORG.org_id)
        self.first.upsert(ALICE, "role", "人工编辑的岗位")
        self.assertIsNone(self.first.apply_auto_fact(job, alice["lease_token"], "role", "我是设计师"))
        self.assertEqual(self.first.list_for(ALICE)[0]["value"], "人工编辑的岗位")
        self.first.discard_conversation_auto(ALICE, "c1")
        self.assertEqual(self.first.suggestion_job_count(ALICE), 0)
        self.assertEqual(self.first.suggestion_job_count(OTHER_ORG), 1)
        self.assertIsNone(self.second.apply_auto_fact(job, alice["lease_token"], "role", "我是设计师"))
        self.first.set_auto_suggest(OTHER_ORG, False)
        self.assertEqual(self.first.suggestion_job_count(OTHER_ORG), 0)
        self.assertIsNone(self.second.apply_auto_fact(other_job, other["lease_token"], "task", "我负责其他组织项目"))

    def test_source_delete_removes_fact_and_job_in_one_transaction(self):
        job = self._enqueue(ALICE, "我负责知识库迁移", "source")
        claim = self.first.claim_suggestion_job()
        self.assertIsNotNone(self.first.apply_auto_fact(job, claim["lease_token"], "task", "我负责知识库迁移"))
        self.assertEqual(self.first.discard_conversation_auto(BOB, "source"), 0)
        self.assertEqual(len(self.first.list_for(ALICE)), 1)
        self.assertEqual(self.first.discard_conversation_auto(ALICE, "source"), 1)
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertEqual(self.first.suggestion_job_count(ALICE), 0)

    def test_expired_raw_question_is_removed(self):
        job = self._enqueue(ALICE, "我负责知识库迁移", "old")
        self.first.connection.execute("UPDATE memory_job SET created_at = now() - interval '25 hours' WHERE id = %s", (job,))
        self.second.recover_suggestion_jobs()
        self.assertEqual(self.first.suggestion_job_count(ALICE), 0)
        self.assertEqual(self.first.list_suggestion_failures(ALICE)[0]["reason_code"], "job_expired")

    def _save_auto(self, kind, value, question, source):
        job = self._enqueue(ALICE, question, source)
        claim = self.first.claim_suggestion_job()
        self.assertEqual(claim["id"], job)
        self.assertIsNotNone(self.first.apply_auto_fact(job, claim["lease_token"], kind, value))
        self.assertTrue(self.first.finish_suggestion_job(job, claim["lease_token"]))
        return self.first.list_for(ALICE)[0]

    def test_retraction_requires_snapshot_lease_and_explicit_evidence(self):
        old_text = "我长期负责门店巡检系统上线"
        old = self._save_auto("task", "长期负责门店巡检系统上线", old_text, "old")
        job = self._enqueue(ALICE, "门店巡检系统上线已经移交同事", "new")
        claim = self.first.claim_suggestion_job()
        self.assertFalse(self.first.retract_auto_fact(job, "stale-token", old, "已经移交"))
        self.assertFalse(self.first.retract_auto_fact(job, claim["lease_token"], old, "凭空证据"))
        self.assertTrue(self.first.retract_auto_fact(job, claim["lease_token"], old, "已经移交"))
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertFalse(self.first.retract_auto_fact(job, claim["lease_token"], old, "已经移交"))

    def test_same_conversation_retraction_and_stale_job_cannot_restore(self):
        old_text = "我长期负责数据平台迁移项目"
        old = self._save_auto("task", "长期负责数据平台迁移项目", old_text, "same")
        stale = self._enqueue(ALICE, old_text, "other")
        self.first.connection.execute("""UPDATE memory_job SET available_at = now() +
            interval '1 hour' WHERE id = %s""", (stale,))
        job = self._enqueue(ALICE, "这个项目已经结束了", "same", (old_text,))
        claim = self.first.claim_suggestion_job()
        self.assertEqual(claim["id"], job)
        self.assertTrue(self.first.retract_auto_fact(job, claim["lease_token"], old, "已经结束了"))
        self.first.connection.execute("""UPDATE memory_job SET available_at = now() -
            interval '1 second' WHERE id = %s""", (stale,))
        old_claim = self.second.claim_suggestion_job()
        self.assertEqual(old_claim["id"], stale)
        self.assertIsNone(self.second.apply_auto_fact(stale, old_claim["lease_token"],
                                                      "task", "长期负责数据平台迁移项目"))
        self.assertEqual(self.first.list_for(ALICE), [])

    def test_changed_snapshot_and_manual_fact_are_protected(self):
        old = self._save_auto("role", "我是测试工程师", "我是测试工程师", "old")
        job = self._enqueue(ALICE, "我不再是测试工程师了", "new")
        claim = self.first.claim_suggestion_job()
        self.first.upsert(ALICE, "role", "手工记录的岗位")
        self.assertFalse(self.first.retract_auto_fact(job, claim["lease_token"], old,
                                                       "我不再是测试工程师了"))
        self.assertEqual(self.first.list_for(ALICE)[0]["value"], "手工记录的岗位")
        self.first.delete(ALICE, "role")
        self.assertIsNone(self.first.apply_auto_fact(job, claim["lease_token"],
                                                      "role", "测试工程师"))
        self.assertEqual(self.first.list_for(ALICE), [])

    def test_cross_session_role_and_task_phase_guards(self):
        role = self._save_auto("role", "我是测试工程师", "我是测试工程师", "old")
        role_job = self._enqueue(ALICE, "我不再是测试工程师了", "new")
        role_claim = self.first.claim_suggestion_job()
        self.assertTrue(self.first.retract_auto_fact(
            role_job, role_claim["lease_token"], role, "我不再是测试工程师了"))
        self.assertTrue(self.first.finish_suggestion_job(role_job, role_claim["lease_token"]))

        task = self._save_auto("task", "长期负责门店巡检系统上线",
                               "我长期负责门店巡检系统上线", "old-task")
        phase_job = self._enqueue(
            ALICE, "门店巡检系统上线一期已结束，但整体项目仍由我负责", "phase")
        phase_claim = self.first.claim_suggestion_job()
        self.assertFalse(self.first.retract_auto_fact(
            phase_job, phase_claim["lease_token"], task, "一期已结束"))
        self.assertEqual(self.first.list_for(ALICE)[0]["value"], task["value"])
        self.assertTrue(self.first.finish_suggestion_job(phase_job, phase_claim["lease_token"]))
        end_job = self._enqueue(ALICE, "门店巡检系统上线已经移交同事", "end")
        end_claim = self.first.claim_suggestion_job()
        self.assertTrue(self.first.retract_auto_fact(
            end_job, end_claim["lease_token"], task, "已经移交"))
        self.assertEqual(self.first.list_for(ALICE), [])

    def test_newer_auto_fact_invalidates_old_snapshot(self):
        old = self._save_auto("task", "长期负责门店巡检系统上线",
                              "我长期负责门店巡检系统上线", "old")
        new_job = self._enqueue(ALICE, "我长期负责财务平台迁移", "changed")
        new_claim = self.first.claim_suggestion_job()
        self.assertIsNotNone(self.first.apply_auto_fact(
            new_job, new_claim["lease_token"], "task", "长期负责财务平台迁移"))
        self.assertTrue(self.first.finish_suggestion_job(new_job, new_claim["lease_token"]))
        retraction = self._enqueue(ALICE, "门店巡检系统上线已经移交同事", "end")
        claim = self.first.claim_suggestion_job()
        self.assertFalse(self.first.retract_auto_fact(
            retraction, claim["lease_token"], old, "已经移交"))
        self.assertEqual(self.first.list_for(ALICE)[0]["value"], "长期负责财务平台迁移")

    def test_source_cleanup_blocks_earlier_other_source_job(self):
        value = "我负责知识库迁移"
        self._save_auto("task", value, value, "deleted-source")
        old_job = self._enqueue(ALICE, value, "different-source")
        self.assertEqual(self.first.discard_conversation_auto(ALICE, "deleted-source"), 1)
        claim = self.second.claim_suggestion_job()
        self.assertEqual(claim["id"], old_job)
        self.assertIsNone(self.second.apply_auto_fact(old_job, claim["lease_token"],
                                                      "task", value))
        self.assertEqual(self.first.list_for(ALICE), [])

    def test_queue_schema_v1_upgrades_without_dropping_jobs(self):
        schema = "memory_test_" + uuid4().hex[:12]
        connection = psycopg.connect(self.dsn, autocommit=True)
        try:
            with connection.transaction():
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                connection.execute(SCHEMA)
                connection.execute(QUEUE_SCHEMA)
                connection.execute("INSERT INTO memory_queue_schema_version(version) VALUES (1)")
                connection.execute("""INSERT INTO memory_job
                    (id, org_id, owner_id, source_conversation_id, question, state,
                     created_at, available_at) VALUES
                    ('existing-job', 'org-a', 'alice', 'old', '我是设计师',
                     'queued', now(), now())""")
            repository = PgUserMemoryRepository(self.dsn, schema=schema)
            try:
                repository.create_schema()
                self.assertEqual(repository.connection.execute(
                    "SELECT version FROM memory_queue_schema_version").fetchone()["version"], 6)
                self.assertIsNotNone(repository.connection.execute(
                    "SELECT to_regclass('memory_fact_guard') AS name").fetchone()["name"])
                self.assertIsNotNone(repository.connection.execute(
                    "SELECT to_regclass('memory_suggestion') AS name").fetchone()["name"])
                self.assertEqual(repository.suggestion_job_count(ALICE), 1)
                self.assertIsNone(repository.claim_suggestion_job())
                self.assertIsNone(repository.next_suggestion_lease_delay())
                self.assertTrue(repository.register_source(ALICE, "old"))
                self.assertIsNone(repository.claim_suggestion_job())
                self.assertIsNone(repository.next_suggestion_lease_delay())
                repository.create_schema()
            finally:
                repository.close()
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            connection.close()

    def test_queue_schema_v4_upgrade_preserves_review_and_active_source(self):
        schema = "memory_test_" + uuid4().hex[:12]
        connection = psycopg.connect(self.dsn, autocommit=True)
        try:
            with connection.transaction():
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                connection.execute(SCHEMA)
                connection.execute(QUEUE_SCHEMA)
                connection.execute(QUEUE_V2)
                connection.execute(QUEUE_V3)
                connection.execute(QUEUE_V4)
                connection.execute("""ALTER TABLE memory_job ADD COLUMN source_validated
                    boolean NOT NULL DEFAULT false""")
                connection.execute("""ALTER TABLE memory_queue_schema_version
                    DROP CONSTRAINT memory_queue_schema_version_version_check""")
                connection.execute("""ALTER TABLE memory_queue_schema_version
                    ADD CONSTRAINT memory_queue_schema_version_version_check CHECK (version = 4)""")
                connection.execute("INSERT INTO memory_queue_schema_version(version) VALUES (4)")
                connection.execute("""INSERT INTO memory_source
                    (org_id, owner_id, id, state, created_at)
                    VALUES ('org-a', 'alice', 'existing', 'active', now())""")
                connection.execute("""INSERT INTO memory_suggestion
                    (id, org_id, owner_id, kind, value, source_conversation_id,
                     source_text, created_at)
                    VALUES ('candidate', 'org-a', 'alice', 'role', '设计师',
                            'existing', '我是设计师', now())""")
            repository = PgUserMemoryRepository(self.dsn, schema=schema)
            try:
                repository.create_schema()
                self.assertEqual(repository.connection.execute(
                    "SELECT version FROM memory_queue_schema_version").fetchone()["version"], 6)
                self.assertEqual(repository.list_suggestions(ALICE)[0]["id"], "candidate")
                self.assertTrue(repository.register_source(ALICE, "existing"))
                repository.set_auto_suggest(ALICE, True)
                self.assertTrue(repository.prepare_source(ALICE, "new"))
                self.assertIsNone(repository.enqueue_suggestion(ALICE, "我是设计师", "new"))
                self.assertTrue(repository.activate_source(ALICE, "new"))
                self.assertIsNotNone(repository.enqueue_suggestion(ALICE, "我是设计师", "new"))
            finally:
                repository.close()
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)))
            connection.close()

    def test_source_must_be_registered_and_deleted_id_cannot_reopen(self):
        question = "我负责知识库迁移"
        self.assertIsNone(self.first.enqueue_suggestion(ALICE, question, "missing"))
        self.assertTrue(self.first.register_source(ALICE, "source"))
        job = self.first.enqueue_suggestion(ALICE, question, "source")
        claim = self.first.claim_suggestion_job()
        self.assertEqual(claim["id"], job)
        self.assertFalse(self.second.delete_source(BOB, "source"))
        self.assertTrue(self.first.delete_source(ALICE, "source"))
        self.assertFalse(self.first.delete_source(ALICE, "source"))
        self.assertFalse(self.first.register_source(ALICE, "source"))
        self.assertIsNone(self.first.enqueue_suggestion(ALICE, question, "source"))
        self.assertIsNone(self.second.apply_auto_fact(job, claim["lease_token"],
                                                      "task", question))
        self.assertEqual(self.first.suggestion_job_count(ALICE), 0)

    def test_delete_source_and_worker_write_serialize(self):
        question = "我是测试工程师"
        job = self._enqueue(ALICE, question, "source")
        claim = self.first.claim_suggestion_job()
        started = Event()

        def worker():
            started.set()
            return self.second.apply_auto_fact(job, claim["lease_token"],
                                               "role", question)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.first.connection.transaction():
                self.first.connection.execute("""SELECT 1 FROM memory_source
                    WHERE org_id = %s AND owner_id = %s AND id = %s FOR UPDATE""",
                    (ALICE.org_id, ALICE.user_id, "source"))
                future = pool.submit(worker)
                self.assertTrue(started.wait(timeout=5))
                self.assertTrue(self.first.delete_source(ALICE, "source"))
                # The other connection remains blocked until this transaction commits.
            self.assertIsNone(future.result(timeout=5))
        self.assertEqual(self.first.list_for(ALICE), [])

    def test_worker_write_before_delete_is_removed_on_commit(self):
        question = "我是测试工程师"
        job = self._enqueue(ALICE, question, "source")
        claim = self.first.claim_suggestion_job()
        started = Event()

        def delete():
            started.set()
            return self.second.delete_source(ALICE, "source")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.first.connection.transaction():
                self.assertIsNotNone(self.first.apply_auto_fact(
                    job, claim["lease_token"], "role", question))
                future = pool.submit(delete)
                self.assertTrue(started.wait(timeout=5))
            self.assertTrue(future.result(timeout=5))
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertEqual(self.first.suggestion_job_count(ALICE), 0)

    def test_same_source_id_is_scoped_to_owner(self):
        self.first.set_auto_suggest(BOB, True)
        alice_job = self._enqueue(ALICE, "我是设计师", "shared")
        bob_job = self._enqueue(BOB, "我是分析师", "shared")
        self.assertTrue(self.first.delete_source(ALICE, "shared"))
        claim = self.second.claim_suggestion_job()
        self.assertEqual(claim["id"], bob_job)
        self.assertNotEqual(claim["id"], alice_job)
        self.assertIsNotNone(self.second.apply_auto_fact(bob_job, claim["lease_token"],
                                                        "role", "我是分析师"))
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertEqual(self.first.list_for(BOB)[0]["value"], "我是分析师")
