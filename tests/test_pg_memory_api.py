"""Opt-in product API contract against isolated synthetic PostgreSQL schemas."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.conversations import ConversationRepository
from app.api.main import create_app
from app.api.pg_user_memories import PgUserMemoryRepository
from app.api.postgres_memory_service import PostgresMemoryService
from app.security.policy import Classification, Subject, UserRole


ALICE = Subject("alice", "org-a", frozenset(), Classification.INTERNAL)
BOB = Subject("bob", "org-a", frozenset(), Classification.INTERNAL)
ADMIN = Subject("admin", "org-a", frozenset(), Classification.INTERNAL,
                role=UserRole.SYSTEM_ADMIN)


class Responder:
    def __init__(self):
        self.profiles = []

    def stream(self, question, history, profile):
        self.profiles.append(profile)
        yield "已收到"


class Suggester:
    def suggest(self, question, context=()):
        return [("role", "我是设计师")] if "我是设计师" in question else []


@unittest.skipUnless(os.getenv("MEMORY_TEST_DATABASE_URL"),
                     "requires dedicated synthetic PostgreSQL test database")
class PgMemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.schema = "memory_test_" + uuid4().hex[:12]
        self.dsn = os.environ["MEMORY_TEST_DATABASE_URL"]
        creator = PgUserMemoryRepository(self.dsn, schema=self.schema)
        creator.create_schema()
        creator.close()
        self.conversations = ConversationRepository(
            Path(self.temp.name) / "conversations.sqlite3", enable_delete_outbox=True)
        self.service = PostgresMemoryService(
            self.dsn, self.conversations, schema=self.schema,
            default_auto_suggest=False)
        self.responder = Responder()
        auth = LocalAuth([LocalAccount("alice", hash_password("password"), ALICE),
                          LocalAccount("bob", hash_password("password"), BOB),
                          LocalAccount("admin", hash_password("password"), ADMIN)])
        self.client = TestClient(create_app(
            auth=auth, conversations=self.conversations, memories=self.service,
            general_responder=self.responder, memory_suggester=Suggester(),
            auto_memory=False))

    def tearDown(self):
        self.service.close()
        self.conversations.close()
        cleanup = PgUserMemoryRepository(self.dsn, schema=self.schema)
        cleanup.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(self.schema)))
        cleanup.close()
        self.temp.cleanup()

    def headers(self, user):
        token = self.client.post("/api/v1/auth/login", json={
            "username": user, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def wait_until(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(predicate())

    def test_crud_review_and_cross_session_profile(self):
        with self.client:
            alice, bob = self.headers("alice"), self.headers("bob")
            admin = self.headers("admin")
            self.assertEqual(self.client.get("/readyz").status_code, 200)
            self.assertEqual(self.client.get("/api/v1/admin/memory-status",
                                             headers=alice).status_code, 403)
            status = self.client.get("/api/v1/admin/memory-status", headers=admin)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["backend"], "postgres")
            self.assertNotIn(ALICE.user_id, str(status.json()))
            self.assertEqual(self.client.put(
                "/api/v1/me/memory-settings", headers=alice,
                json={"auto_suggest": True}).status_code, 200)
            answer = self.client.post("/api/v1/conversations/answers", headers=alice,
                                      json={"question": "我是设计师", "mode": "extract"})
            self.assertEqual(answer.status_code, 200, answer.text)
            source = answer.json()["conversation_id"]
            url = "/api/v1/me/memory-suggestions"
            self.wait_until(lambda: len(self.client.get(url, headers=alice).json()) == 1)
            suggestion = self.client.get(url, headers=alice).json()[0]
            self.assertEqual(self.client.get(url, headers=bob).json(), [])
            self.assertEqual(self.client.post(
                f"{url}/{suggestion['id']}/accept", headers=bob,
                json={"value": "设计负责人"}).status_code, 404)
            confirmed = self.client.post(
                f"{url}/{suggestion['id']}/accept", headers=alice,
                json={"value": "设计负责人"})
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["origin"], "manual")
            self.assertEqual(self.client.get("/api/v1/me/memories", headers=bob).json(), [])
            self.client.post("/api/v1/conversations/answers", headers=alice,
                             json={"question": "解释单元测试", "mode": "extract"})
            self.assertEqual(self.responder.profiles[-1]["role"], "设计负责人")
            self.assertEqual(self.client.delete(
                f"/api/v1/conversations/{source}", headers=alice).status_code, 204)
            self.assertEqual(self.client.get(
                "/api/v1/me/memories", headers=alice).json()[0]["value"], "设计负责人")

    def test_deleted_source_is_hidden_while_target_cleanup_is_unavailable(self):
        with self.client:
            alice = self.headers("alice")
            self.service.set_auto_suggest(ALICE, True)
            source = self.conversations.append(ALICE, None, "我是设计师", "general",
                                               {"status": "general", "paragraphs": ["ok"],
                                                "citations": []})
            self.assertTrue(self.service.enqueue_suggestion(ALICE, "我是设计师", source))
            job = self.service.claim_suggestion_job()
            self.assertIsNotNone(self.service.upsert_auto(
                ALICE, "role", "我是设计师", source, "我是设计师",
                job_lease=(job["id"], job["lease_token"])))
            self.assertTrue(self.service.finish_suggestion_job(job["id"], job["lease_token"]))
            self.assertEqual(len(self.client.get("/api/v1/me/memories", headers=alice).json()), 1)
            self.assertTrue(self.conversations.delete(ALICE, source))
            self.assertEqual(self.client.get("/api/v1/me/memories", headers=alice).json(), [])

    def test_deleted_source_cannot_be_confirmed_as_manual_memory(self):
        with self.client:
            alice = self.headers("alice")
            self.service.set_auto_suggest(ALICE, True)
            source = self.conversations.append(ALICE, None, "我是设计师", "general",
                                               {"status": "general", "paragraphs": ["ok"],
                                                "citations": []})
            self.assertTrue(self.service.enqueue_suggestion(ALICE, "我是设计师", source))
            job = self.service.claim_suggestion_job()
            self.service.propose(
                ALICE, "role", "我是设计师", source, "我是设计师",
                job_lease=(job["id"], job["lease_token"]))
            suggestion = self.service.list_suggestions(ALICE)[0]
            self.assertTrue(self.conversations.delete(ALICE, source))
            response = self.client.post(
                f"/api/v1/me/memory-suggestions/{suggestion['id']}/accept",
                headers=alice, json={"value": "设计负责人"})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(self.client.delete(
                f"/api/v1/me/memory-suggestions/{suggestion['id']}",
                headers=alice).status_code, 404)
            self.assertEqual(self.service.list_for(ALICE), [])
