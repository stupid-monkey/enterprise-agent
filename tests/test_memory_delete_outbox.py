import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from unittest import mock
from uuid import uuid4

from psycopg import sql

from app.api.conversations import ConversationRepository
from app.api.memory_delete_relay import MemoryDeleteRelayWorker, relay_one_delete
from app.api.memory_enqueue_relay import MemoryEnqueueRelayWorker
from app.api.memory_read_barrier import list_memories_with_source_barrier
from app.api.memory_source_registration import register_owned_source
from app.api.pg_user_memories import PgUserMemoryRepository
from app.api.postgres_memory_service import PostgresMemoryService
from app.security.policy import Classification, Subject
from eval.benchmark_memory_delete_relay import benchmark


ALICE = Subject("alice", "org-a", frozenset(), Classification.INTERNAL)
BOB = Subject("bob", "org-a", frozenset(), Classification.INTERNAL)


class ConversationDeleteOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "conversations.sqlite3"
        self.first = ConversationRepository(self.path, enable_delete_outbox=True)
        self.second = ConversationRepository(self.path, enable_delete_outbox=True)

    def tearDown(self):
        self.second.close()
        self.first.close()
        self.temp.cleanup()

    def conversation(self, subject=ALICE):
        return self.first.append(subject, None, "你好", "general", {"status": "ok"})

    def test_delete_and_event_commit_or_rollback_together(self):
        conversation_id = self.conversation()
        self.assertFalse(self.first.delete(BOB, conversation_id))
        self.assertEqual(self.first.pending_delete_count(), 0)
        with mock.patch.object(self.first, "_record_delete_event", side_effect=RuntimeError("fail")):
            with self.assertRaisesRegex(RuntimeError, "fail"):
                self.first.delete(ALICE, conversation_id)
        self.assertTrue(self.first.owns(ALICE, conversation_id))
        self.assertEqual(self.first.pending_delete_count(), 0)
        self.assertTrue(self.first.delete(ALICE, conversation_id))
        self.assertFalse(self.first.owns(ALICE, conversation_id))
        self.assertEqual(self.first.pending_delete_count(), 1)
        self.assertFalse(self.first.delete(ALICE, conversation_id))
        self.assertEqual(self.first.pending_delete_count(), 1)

    def test_merge_records_source_deletion_only(self):
        source = self.conversation()
        target = self.conversation()
        foreign = self.conversation(BOB)
        self.assertFalse(self.first.merge(ALICE, target, foreign))
        self.assertTrue(self.first.merge(ALICE, target, source))
        event = self.second.claim_delete_event()
        self.assertEqual(event["conversation_id"], source)
        self.assertEqual(event["owner_id"], ALICE.user_id)
        self.assertTrue(self.first.owns(ALICE, target))
        self.assertTrue(self.first.owns(BOB, foreign))

    def test_pending_event_survives_repository_restart(self):
        source = self.conversation()
        self.assertTrue(self.first.delete(ALICE, source))
        self.first.close()
        self.first = ConversationRepository(self.path, enable_delete_outbox=True)
        event = self.first.claim_delete_event()
        self.assertEqual(event["conversation_id"], source)
        self.assertTrue(self.first.ack_delete_event(event["id"], event["lease_token"]))
        self.assertEqual(self.second.pending_delete_count(), 0)

    def test_two_connections_claim_once_then_retry_after_lease(self):
        conversation_id = self.conversation()
        self.assertTrue(self.first.delete(ALICE, conversation_id))
        barrier = Barrier(2)

        def claim(repository):
            barrier.wait()
            return repository.claim_delete_event()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = (pool.submit(claim, self.first), pool.submit(claim, self.second))
            results = [item.result(timeout=10) for item in claims]
        claimed = next(item for item in results if item is not None)
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertFalse(self.first.ack_delete_event(claimed["id"], "wrong"))
        self.assertFalse(self.first.retry_delete_event(claimed["id"], "wrong", "target_error"))
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?""", (claimed["id"],))
        reclaimed = self.second.claim_delete_event()
        self.assertEqual(reclaimed["id"], claimed["id"])
        self.assertNotEqual(reclaimed["lease_token"], claimed["lease_token"])
        self.assertFalse(self.first.ack_delete_event(claimed["id"], claimed["lease_token"]))
        self.assertTrue(self.second.ack_delete_event(reclaimed["id"], reclaimed["lease_token"]))
        self.assertEqual(self.first.pending_delete_count(), 0)

    def test_metrics_show_backlog_without_account_identifiers(self):
        self.assertEqual(self.first.delete_outbox_metrics()["pending"], 0)
        source = self.conversation()
        self.first.delete(ALICE, source)
        waiting = self.first.delete_outbox_metrics()
        self.assertEqual(waiting["pending"], 1)
        self.assertEqual(waiting["in_flight"], 0)
        self.assertIsNotNone(waiting["oldest_pending_seconds"])
        self.assertNotIn(ALICE.user_id, str(waiting))
        claim = self.first.claim_delete_event()
        self.assertEqual(self.first.delete_outbox_metrics()["in_flight"], 1)
        self.assertTrue(self.first.retry_delete_event(
            claim["id"], claim["lease_token"], "target_unavailable"))
        self.assertEqual(self.first.delete_outbox_metrics()["failed_pending"], 1)
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET available_at = '2000-01-01T00:00:00+00:00' WHERE id = ?""", (claim["id"],))
        again = self.first.claim_delete_event()
        self.assertEqual(self.first.delete_outbox_metrics()["retried"], 1)
        self.assertTrue(self.first.ack_delete_event(again["id"], again["lease_token"]))
        done = self.first.delete_outbox_metrics()
        self.assertEqual(done["pending"], 0)
        self.assertEqual(done["delivered_last_hour"], 1)

    def test_memory_event_commits_with_turn_and_keeps_only_turn_reference(self):
        source = self.first.append(ALICE, None, "我长期负责知识库迁移", "general",
                                   {"paragraphs": ["ok"], "citations": []},
                                   memory_include_ordinary=True)
        self.first.append(ALICE, source, "这个项目已经结束了", "general",
                          {"paragraphs": ["ok"], "citations": []},
                          memory_include_ordinary=True)
        self.assertEqual(self.first.pending_memory_event_count(), 2)
        row = self.first._db.execute("""SELECT * FROM conversation_memory_outbox
            ORDER BY turn_id LIMIT 1""").fetchone()
        self.assertNotIn("question", row.keys())
        first = self.second.claim_memory_event()
        self.assertEqual(first["question"], "我长期负责知识库迁移")
        self.assertEqual(first["context"], ())
        self.assertFalse(self.first.ack_memory_event(first["id"], "wrong-token"))
        self.assertTrue(self.first.ack_memory_event(first["id"], first["lease_token"]))
        second = self.second.claim_memory_event()
        self.assertEqual(second["context"], ("我长期负责知识库迁移",))
        self.assertTrue(self.first.delete(ALICE, source))
        self.assertEqual(self.first.pending_memory_event_count(), 0)
        self.assertFalse(self.second.ack_memory_event(second["id"], second["lease_token"]))

    def test_memory_event_insert_failure_rolls_back_new_conversation(self):
        with self.first._db:
            self.first._db.execute("""CREATE TRIGGER reject_memory_event
                BEFORE INSERT ON conversation_memory_outbox
                BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic failure"):
            self.first.append(ALICE, None, "我是设计师", "general",
                              {"paragraphs": ["ok"], "citations": []},
                              memory_include_ordinary=True)
        self.assertEqual(self.first.list_for(ALICE), [])
        self.assertEqual(self.first.pending_memory_event_count(), 0)

    def test_memory_event_retry_and_reclaim_keeps_one_event(self):
        source = self.conversation()
        self.first.append(ALICE, source, "我是设计师", "general",
                          {"paragraphs": ["ok"], "citations": []},
                          memory_include_ordinary=False)
        claimed = self.first.claim_memory_event()
        self.assertEqual(claimed["include_ordinary"], 0)
        self.assertTrue(self.first.retry_memory_event(
            claimed["id"], claimed["lease_token"], "target_unavailable"))
        self.assertEqual(self.first.pending_memory_event_count(), 1)
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_memory_outbox
                SET available_at = '2000-01-01T00:00:00+00:00'
                WHERE id = ?""", (claimed["id"],))
        again = self.second.claim_memory_event()
        self.assertEqual(again["id"], claimed["id"])
        self.assertNotEqual(again["lease_token"], claimed["lease_token"])
        self.assertFalse(self.first.ack_memory_event(claimed["id"], claimed["lease_token"]))
        self.assertTrue(self.first.ack_memory_event(again["id"], again["lease_token"]))
        self.assertEqual(self.first.pending_memory_event_count(), 0)


@unittest.skipUnless(os.getenv("MEMORY_TEST_DATABASE_URL"),
                     "requires dedicated synthetic PostgreSQL test database")
class PgDeleteRelayTests(ConversationDeleteOutboxTests):
    def setUp(self):
        super().setUp()
        self.schema = "memory_test_" + uuid4().hex[:12]
        self.pg = PgUserMemoryRepository(os.environ["MEMORY_TEST_DATABASE_URL"], schema=self.schema)
        self.pg.create_schema()
        self.pg.set_auto_suggest(ALICE, True)
        self.pg.set_auto_suggest(BOB, True)

    def tearDown(self):
        self.pg.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(self.schema)))
        self.pg.close()
        super().tearDown()

    def _auto_fact(self, subject, conversation_id, value):
        self.pg.register_source(subject, conversation_id)
        job = self.pg.enqueue_suggestion(subject, value, conversation_id)
        claim = self.pg.claim_suggestion_job()
        self.assertEqual(claim["id"], job)
        self.assertIsNotNone(self.pg.apply_auto_fact(job, claim["lease_token"], "role", value))
        self.assertTrue(self.pg.finish_suggestion_job(job, claim["lease_token"]))

    def test_source_registration_requires_authoritative_owner_and_outbox(self):
        source = self.conversation()
        self.assertFalse(register_owned_source(BOB, source, self.first, self.pg))
        self.assertFalse(register_owned_source(ALICE, "missing", self.first, self.pg))
        without_outbox = ConversationRepository(self.path)
        try:
            with self.assertRaisesRegex(RuntimeError, "CONVERSATION_DELETE_OUTBOX_REQUIRED"):
                register_owned_source(ALICE, source, without_outbox, self.pg)
        finally:
            without_outbox.close()
        self.assertTrue(register_owned_source(ALICE, source, self.first, self.pg))
        self.assertTrue(register_owned_source(ALICE, source, self.first, self.pg))
        self.assertIsNotNone(self.pg.enqueue_suggestion(ALICE, "我是设计师", source))

    def test_deletion_during_source_registration_tombstones_before_return(self):
        source = self.conversation()
        original_owns = self.first.owns
        reads = 0

        def delete_on_second_read(subject, conversation_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.assertTrue(self.second.delete(ALICE, source))
            return original_owns(subject, conversation_id)

        with mock.patch.object(self.first, "owns", side_effect=delete_on_second_read):
            self.assertFalse(register_owned_source(ALICE, source, self.first, self.pg))
        self.assertEqual(self.first.pending_delete_count(), 1)
        self.assertFalse(self.pg.register_source(ALICE, source))
        self.assertIsNone(self.pg.enqueue_suggestion(ALICE, "我是设计师", source))
        self.assertEqual(relay_one_delete(self.first, self.pg), "delivered")

    def test_second_source_read_failure_tombstones_registration(self):
        source = self.conversation()
        original_owns = self.first.owns
        reads = 0

        def fail_on_second_read(subject, conversation_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise RuntimeError("SOURCE_READ_UNAVAILABLE")
            return original_owns(subject, conversation_id)

        with mock.patch.object(self.first, "owns", side_effect=fail_on_second_read):
            with self.assertRaisesRegex(RuntimeError, "SOURCE_READ_UNAVAILABLE"):
                register_owned_source(ALICE, source, self.first, self.pg)
        self.assertFalse(self.pg.register_source(ALICE, source))

    def test_pending_source_cannot_enqueue_if_cleanup_also_fails(self):
        source = self.conversation()
        original_owns = self.first.owns
        reads = 0

        def fail_on_second_read(subject, conversation_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise RuntimeError("SOURCE_READ_UNAVAILABLE")
            return original_owns(subject, conversation_id)

        with mock.patch.object(self.first, "owns", side_effect=fail_on_second_read):
            with mock.patch.object(self.pg, "discard_conversation_auto",
                                   side_effect=RuntimeError("TARGET_CLEANUP_FAILED")):
                with self.assertRaisesRegex(RuntimeError, "TARGET_CLEANUP_FAILED"):
                    register_owned_source(ALICE, source, self.first, self.pg)
        state = self.pg.connection.execute("""SELECT state FROM memory_source
            WHERE org_id = %s AND owner_id = %s AND id = %s""",
            (ALICE.org_id, ALICE.user_id, source)).fetchone()["state"]
        self.assertEqual(state, "pending")
        self.assertIsNone(self.pg.enqueue_suggestion(ALICE, "我是设计师", source))
        self.assertFalse(self.pg.register_source(ALICE, source))
        self.assertTrue(register_owned_source(ALICE, source, self.first, self.pg))
        self.assertIsNotNone(self.pg.enqueue_suggestion(ALICE, "我是设计师", source))

    def test_memory_enqueue_relay_retries_and_crash_replay_is_idempotent(self):
        source = self.first.append(ALICE, None, "我是设计师", "general",
                                   {"paragraphs": ["ok"], "citations": []},
                                   memory_include_ordinary=False)
        service = PostgresMemoryService(os.environ["MEMORY_TEST_DATABASE_URL"], self.first,
                                        schema=self.schema)
        try:
            relay = MemoryEnqueueRelayWorker(self.first, service)
            with mock.patch.object(service, "enqueue_suggestion",
                                   side_effect=RuntimeError("target offline")):
                self.assertEqual(relay.run_once(), "retry")
            self.assertEqual(self.first.pending_memory_event_count(), 1)
            with self.first._db:
                self.first._db.execute("""UPDATE conversation_memory_outbox
                    SET available_at = '2000-01-01T00:00:00+00:00'
                    WHERE delivered_at IS NULL""")
            with mock.patch.object(self.first, "ack_memory_event",
                                   side_effect=RuntimeError("crash after target commit")):
                with self.assertRaisesRegex(RuntimeError, "crash after target commit"):
                    relay.run_once()
            self.assertEqual(self.pg.suggestion_job_count(ALICE), 1)
            self.assertEqual(self.first.pending_memory_event_count(), 1)
            with self.first._db:
                self.first._db.execute("""UPDATE conversation_memory_outbox
                    SET lease_until = '2000-01-01T00:00:00+00:00'
                    WHERE delivered_at IS NULL""")
            self.assertEqual(relay.run_once(), "delivered")
            self.assertEqual(self.pg.suggestion_job_count(ALICE), 1)
            self.assertEqual(self.first.pending_memory_event_count(), 0)
            self.assertTrue(self.first.delete(ALICE, source))
            self.assertEqual(relay.run_once(), "idle")
        finally:
            service.close()

    def test_relay_clears_only_deleted_owners_fact(self):
        source = self.conversation()
        self._auto_fact(ALICE, source, "我是设计师")
        self._auto_fact(BOB, source, "我是分析师")
        self.assertTrue(self.first.delete(ALICE, source))
        self.assertEqual(len(self.pg.list_for(ALICE)), 1)
        self.assertEqual(relay_one_delete(self.first, self.pg), "delivered")
        self.assertEqual(self.pg.list_for(ALICE), [])
        self.assertEqual(self.pg.list_for(BOB)[0]["value"], "我是分析师")
        self.assertEqual(relay_one_delete(self.first, self.pg), "idle")
        self.assertFalse(self.pg.register_source(ALICE, source))

    def test_read_barrier_hides_pending_delete_but_keeps_manual_memory(self):
        source = self.conversation()
        self._auto_fact(ALICE, source, "我是设计师")
        self.pg.upsert(ALICE, "answer_preference", "简短回答")
        self.assertEqual(len(list_memories_with_source_barrier(ALICE, self.pg, self.second)), 2)
        self.assertTrue(self.first.delete(ALICE, source))
        # The target has not received the outbox event yet.
        self.assertEqual(len(self.pg.list_for(ALICE)), 2)
        visible = list_memories_with_source_barrier(ALICE, self.pg, self.second)
        self.assertEqual([(fact["kind"], fact["value"]) for fact in visible],
                         [("answer_preference", "简短回答")])
        self.assertEqual(self.first.pending_delete_count(), 1)
        with mock.patch.object(self.pg, "discard_conversation_auto", side_effect=RuntimeError("offline")):
            self.assertEqual(relay_one_delete(self.first, self.pg), "retry")
        self.assertEqual([(fact["kind"], fact["value"]) for fact in
                          list_memories_with_source_barrier(ALICE, self.pg, self.second)],
                         [("answer_preference", "简短回答")])
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET available_at = '2000-01-01T00:00:00+00:00' WHERE delivered_at IS NULL""")
        self.assertEqual(relay_one_delete(self.first, self.pg), "delivered")
        self.assertEqual(len(self.pg.list_for(ALICE)), 1)

    def test_crash_after_postgres_commit_replays_idempotently(self):
        source = self.conversation()
        self._auto_fact(ALICE, source, "我是设计师")
        self.assertTrue(self.first.delete(ALICE, source))
        event = self.first.claim_delete_event()
        self.pg.discard_conversation_auto(ALICE, source)
        self.assertEqual(self.pg.list_for(ALICE), [])
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?""", (event["id"],))
        self.assertEqual(relay_one_delete(self.second, self.pg), "delivered")
        self.assertEqual(self.first.pending_delete_count(), 0)
        self.assertFalse(self.pg.register_source(ALICE, source))

    def test_target_failure_retries_without_losing_event(self):
        source = self.conversation()
        self._auto_fact(ALICE, source, "我是设计师")
        self.assertTrue(self.first.delete(ALICE, source))
        with mock.patch.object(self.pg, "discard_conversation_auto", side_effect=RuntimeError("offline")):
            self.assertEqual(relay_one_delete(self.first, self.pg), "retry")
        self.assertEqual(self.first.pending_delete_count(), 1)
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET available_at = '2000-01-01T00:00:00+00:00' WHERE delivered_at IS NULL""")
        self.assertEqual(relay_one_delete(self.second, self.pg), "delivered")
        self.assertEqual(self.pg.list_for(ALICE), [])

    def test_worker_restart_replays_after_target_commit_before_ack(self):
        source = self.conversation()
        self._auto_fact(ALICE, source, "我是设计师")
        self.first.delete(ALICE, source)

        def factory():
            return PgUserMemoryRepository(os.environ["MEMORY_TEST_DATABASE_URL"],
                                          schema=self.schema)

        worker = MemoryDeleteRelayWorker(self.first, factory)
        with mock.patch.object(self.first, "ack_delete_event", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                worker.run_once()
        self.assertEqual(self.pg.list_for(ALICE), [])
        self.assertEqual(self.first.pending_delete_count(), 1)
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET lease_until = '2000-01-01T00:00:00+00:00'
                WHERE delivered_at IS NULL""")
        self.assertEqual(MemoryDeleteRelayWorker(self.second, factory).run_once(), "delivered")
        self.assertEqual(self.first.pending_delete_count(), 0)
        self.assertFalse(self.pg.register_source(ALICE, source))

    def test_online_worker_reconnects_and_reports_aggregate_progress(self):
        source = self.conversation()
        self._auto_fact(ALICE, source, "我是设计师")
        self.first.delete(ALICE, source)
        attempts = 0
        snapshots = []

        def factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic target failure")
            return PgUserMemoryRepository(os.environ["MEMORY_TEST_DATABASE_URL"],
                                          schema=self.schema)

        worker = MemoryDeleteRelayWorker(self.first, factory, poll_seconds=0.01,
                                         observe=snapshots.append)
        self.assertEqual(worker.run_once(), "retry")
        self.assertEqual(self.first.delete_outbox_metrics()["pending"], 1)
        with self.first._db:
            self.first._db.execute("""UPDATE conversation_delete_outbox
                SET available_at = '2000-01-01T00:00:00+00:00'
                WHERE delivered_at IS NULL""")
        stop = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker.run_until_stopped, stop)
            deadline = monotonic() + 5
            while self.first.pending_delete_count() and monotonic() < deadline:
                sleep(0.01)
            stop.set()
            future.result(timeout=5)
        self.assertEqual(self.first.pending_delete_count(), 0)
        self.assertEqual(self.pg.list_for(ALICE), [])
        self.assertGreaterEqual(attempts, 2)
        self.assertTrue(any(item["status"] == "delivered" for item in snapshots))
        self.assertNotIn(ALICE.user_id, str(snapshots))


@unittest.skipUnless(os.getenv("MEMORY_TEST_DATABASE_URL"),
                     "requires dedicated synthetic PostgreSQL test database")
class PgDeleteRelayConcurrencyTests(unittest.TestCase):
    def test_four_consumers_deliver_each_event_once(self):
        result = benchmark(os.environ["MEMORY_TEST_DATABASE_URL"], events=12, workers=4)
        self.assertEqual(result["outcomes"].get("delivered"), 12)
        self.assertEqual(result["retried_events"], 0)
        self.assertEqual(result["pending_at_end"], 0)
        self.assertEqual(result["remaining_facts"], 0)
        self.assertEqual(result["deleted_source_tombstones"], 12)

    def test_four_consumers_recover_after_shared_target_outage(self):
        result = benchmark(os.environ["MEMORY_TEST_DATABASE_URL"], events=12,
                           workers=4, target_outage_seconds=1.5)
        self.assertGreater(result["outcomes"].get("retry", 0), 0)
        self.assertEqual(result["outcomes"].get("delivered"), 12)
        self.assertEqual(result["pending_at_end"], 0)
        self.assertEqual(result["remaining_facts"], 0)
        self.assertEqual(result["deleted_source_tombstones"], 12)
