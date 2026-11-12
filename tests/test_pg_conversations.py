"""PostgreSQL conversation contract in a disposable laboratory schema."""

import os
import threading
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.api.memory_monitoring import postgres_outbox_snapshot
from app.api.pg_conversations import PgConversationRepository
from app.security.policy import Classification, Subject


class PgConversationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.getenv("MEMORY_TEST_DATABASE_URL", "")
        if not cls.dsn:
            raise unittest.SkipTest("MEMORY_TEST_DATABASE_URL required")
        if psycopg.conninfo.conninfo_to_dict(cls.dsn).get("dbname") != "agent_memory_lab":
            raise unittest.SkipTest("dedicated memory lab database required")

    def setUp(self):
        self.schema = "conversation_test_" + uuid4().hex[:12]
        self.first = PgConversationRepository(self.dsn, schema=self.schema, create_schema=True)
        self.second = PgConversationRepository(self.dsn, schema=self.schema)
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.alice = Subject("alice", "org-one", frozenset(), Classification.INTERNAL)
        self.bob = Subject("bob", "org-one", frozenset(), Classification.INTERNAL)
        self.other_org = Subject("alice", "org-two", frozenset(), Classification.INTERNAL)

    def tearDown(self):
        self.first.close()
        self.second.close()
        self.documents.close()
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                sql.Identifier(self.schema)))

    def test_owner_history_merge_and_delete_event(self):
        first_id = self.first.append(self.alice, None, "第一题", "general",
                                     {"status": "ordinary", "paragraphs": ["一"]},
                                     memory_include_ordinary=False)
        second_id = self.first.append(self.alice, None, "第二题", "general",
                                      {"status": "ordinary", "paragraphs": ["二"]})
        self.assertTrue(self.second.owns(self.alice, first_id))
        self.assertFalse(self.second.owns(self.bob, first_id))
        self.assertFalse(self.second.owns(self.other_org, first_id))
        self.assertEqual(len(self.second.list_for(self.alice)), 2)
        self.assertIsNone(self.second.get(self.bob, first_id, self.documents))
        self.assertFalse(self.second.merge(self.bob, second_id, first_id))
        self.assertTrue(self.second.merge(self.alice, second_id, first_id))
        self.assertEqual([turn["question"] for turn in
                          self.first.get(self.alice, second_id, self.documents)["turns"]],
                         ["第一题", "第二题"])
        self.assertEqual(self.first.pending_delete_count(), 1)
        self.assertFalse(self.first.delete(self.bob, second_id))
        self.assertTrue(self.first.delete(self.alice, second_id))
        self.assertFalse(self.first.delete(self.alice, second_id))
        self.assertEqual(self.second.pending_delete_count(), 2)
        self.assertEqual(self.second.owned_source_ids(self.alice, {first_id, second_id}), set())

    def test_turn_and_enqueue_event_commit_together(self):
        conversation_id = self.first.append(
            self.alice, None, "我目前是数据分析师。", "general",
            {"status": "ordinary", "paragraphs": ["好"]},
            memory_include_ordinary=True)
        self.assertEqual(self.second.pending_memory_event_count(), 1)
        event = self.second.claim_memory_event()
        self.assertEqual(event["conversation_id"], conversation_id)
        self.assertEqual(event["question"], "我目前是数据分析师。")
        self.assertEqual(event["context"], ())
        self.assertIsNone(self.first.claim_memory_event())
        self.assertFalse(self.first.ack_memory_event(event["id"], "wrong-token"))
        self.assertTrue(self.first.ack_memory_event(event["id"], event["lease_token"]))
        self.assertFalse(self.second.ack_memory_event(event["id"], event["lease_token"]))
        self.assertEqual(self.first.pending_memory_event_count(), 0)

    def test_delete_discards_unhandled_memory_event_and_leases_delete(self):
        conversation_id = self.first.append(
            self.alice, None, "我目前是数据分析师。", "general",
            {"status": "ordinary"}, memory_include_ordinary=True)
        self.assertEqual(self.first.pending_memory_event_count(), 1)
        self.assertTrue(self.second.delete(self.alice, conversation_id))
        self.assertEqual(self.first.pending_memory_event_count(), 0)
        event = self.first.claim_delete_event()
        self.assertEqual(event["conversation_id"], conversation_id)
        self.assertIsNone(self.second.claim_delete_event())
        self.assertFalse(self.second.ack_delete_event(event["id"], "wrong-token"))
        self.assertTrue(self.second.ack_delete_event(event["id"], event["lease_token"]))
        self.assertEqual(self.first.pending_delete_count(), 0)
        self.assertEqual(self.second.delete_outbox_metrics()["delivered_last_hour"], 1)

    def test_source_guard_blocks_concurrent_delete(self):
        conversation_id = self.first.append(self.alice, None, "问题", "general", {})
        entered = threading.Event()
        done = threading.Event()
        result = []

        def delete():
            entered.set()
            result.append(self.second.delete(self.alice, conversation_id))
            done.set()

        with self.first.hold_owned_source(self.alice, conversation_id) as owned:
            self.assertTrue(owned)
            worker = threading.Thread(target=delete)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(done.wait(0.15))
        worker.join(timeout=3)
        self.assertEqual(result, [True])
        self.assertFalse(self.first.owns(self.alice, conversation_id))

    def test_workflow_run_is_idempotent(self):
        first_id = self.first.append(self.alice, None, "原题", "extract", {},
                                     workflow_run_id="run-one")
        repeated = self.second.append(self.alice, None, "重复题", "extract", {},
                                      workflow_run_id="run-one")
        self.assertEqual(first_id, repeated)
        self.assertEqual(self.first.conversation_for_run(self.alice, "run-one"), first_id)
        self.assertEqual(self.first.list_for(self.alice)[0]["turn_count"], 1)

    def test_api_injection_and_postgres_monitor_counts(self):
        conversation_id = self.first.append(
            self.alice, None, "我目前是风控分析师。", "general",
            {"status": "ordinary", "paragraphs": ["收到"], "citations": []},
            memory_include_ordinary=True)
        snapshot = postgres_outbox_snapshot(self.dsn, self.schema)
        self.assertEqual(snapshot["enqueue"]["pending"], 1)
        self.assertEqual(snapshot["delete"]["pending"], 0)
        auth = LocalAuth([LocalAccount("alice", hash_password("test-password"), self.alice)])
        client = TestClient(create_app(auth, self.documents, conversations=self.second))
        self.assertEqual(client.get("/readyz").status_code, 200)
        token = client.post("/api/v1/auth/login", json={
            "username": "alice", "password": "test-password"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(client.get("/api/v1/conversations", headers=headers).json()[0]["id"],
                         conversation_id)
        self.assertEqual(client.get(f"/api/v1/conversations/{conversation_id}",
                                    headers=headers).json()["turns"][0]["question"],
                         "我目前是风控分析师。")
        self.assertEqual(client.delete(f"/api/v1/conversations/{conversation_id}",
                                       headers=headers).status_code, 204)
        snapshot = postgres_outbox_snapshot(self.dsn, self.schema)
        self.assertEqual(snapshot["enqueue"]["pending"], 0)
        self.assertEqual(snapshot["delete"]["pending"], 1)

    def test_concurrent_claim_has_no_duplicate_events(self):
        for index in range(40):
            self.first.append(self.alice, None, f"我目前是数据分析师{index}。",
                              "general", {}, memory_include_ordinary=True)
        claimed = []
        mutex = threading.Lock()

        def worker():
            repository = PgConversationRepository(self.dsn, schema=self.schema)
            try:
                while True:
                    event = repository.claim_memory_event()
                    if event is None:
                        return
                    with mutex:
                        claimed.append(event["id"])
                    self.assertTrue(repository.ack_memory_event(
                        event["id"], event["lease_token"]))
            finally:
                repository.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(claimed), 40)
        self.assertEqual(len(set(claimed)), 40)
        self.assertEqual(self.first.pending_memory_event_count(), 0)

    def test_concurrent_workflow_repeat_is_single_turn(self):
        results = []
        mutex = threading.Lock()

        def worker(repository):
            result = repository.append(self.alice, None, "相同任务", "extract", {},
                                       workflow_run_id="shared-run")
            with mutex:
                results.append(result)

        threads = [threading.Thread(target=worker, args=(repository,))
                   for repository in (self.first, self.second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.first.list_for(self.alice)[0]["turn_count"], 1)


if __name__ == "__main__":
    unittest.main()
