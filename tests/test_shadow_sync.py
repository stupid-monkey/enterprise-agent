"""Durable shadow-index outbox and at-least-once delivery behavior."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.documents import DocumentRepository
from app.api.shadow_sync import drain_index_outbox
from app.security.policy import Classification, Subject, UserRole, Visibility


class FakeEncoder:
    def encode(self, sentences, *, normalize_embeddings, show_progress_bar):
        return [[0.0] * 512 for _ in sentences]


class FakeSink:
    def __init__(self):
        self.rows = {}
        self.fail_next = False

    def upsert_document(self, document, segments, *, event_id):
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("offline")
        old = self.rows.get(document["document_id"])
        if old is not None and old["event_id"] >= event_id:
            return False
        self.rows[document["document_id"]] = {
            "event_id": event_id, "version": document["version"],
            "state": document["state"], "segments": segments,
            "denied": document["denied_user_ids"],
        }
        return True


class ShadowSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "documents.sqlite3"
        self.docs = DocumentRepository(self.path, seed_demo=True, record_index_events=True)
        self.encoder = FakeEncoder()
        self.sink = FakeSink()
        self.manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                               Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)

    def tearDown(self):
        self.docs.close()
        self.directory.cleanup()

    def test_bootstrap_and_mutations_are_recorded_once(self):
        self.assertEqual(len(self.docs.pending_index_events()), 6)
        self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder)["processed"], 6)
        identifier = self.docs.create_upload(
            self.manager, title="采购流程", filename="procurement.txt", file_type="TXT",
            area="制度规范", visibility=Visibility.ORGANIZATION,
            content=b"contract approval", extracted_text="contract approval")
        self.assertEqual(len(self.docs.pending_index_events()), 1)
        self.assertFalse(self.docs.publish(Subject("employee-001", "demo-org",
            frozenset({"general"}), Classification.INTERNAL), identifier))
        self.assertEqual(len(self.docs.pending_index_events()), 1)
        self.assertTrue(self.docs.publish(self.manager, identifier))
        self.assertTrue(self.docs.set_denied("demo-org", identifier, "employee-001", True))
        revision = self.docs.create_revision(self.manager, identifier, filename="procurement.txt",
            file_type="TXT", content=b"new approval", extracted_text="new approval")
        self.assertEqual(revision, "v1.1")
        self.assertEqual(self.docs.publish_revision(self.manager, identifier), revision)
        self.assertEqual(len(self.docs.pending_index_events()), 4)
        self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder)["processed"], 4)
        self.assertEqual(self.sink.rows[identifier]["version"], "v1.1")
        self.assertEqual(self.sink.rows[identifier]["denied"], ["employee-001"])

    def test_failure_survives_restart_and_retry(self):
        source_id = self.docs.index_source_id
        self.sink.fail_next = True
        self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder, max_events=1)["failed"], 1)
        self.assertEqual(self.docs.pending_index_events(limit=1)[0]["attempts"], 1)
        self.docs.close()
        self.docs = DocumentRepository(self.path, seed_demo=True, record_index_events=True)
        self.assertEqual(self.docs.index_source_id, source_id)
        self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder, max_events=1)["processed"], 1)
        self.assertEqual(self.docs.pending_index_events(limit=20)[0]["event_id"], 2)

    def test_replay_after_postgres_commit_is_idempotent(self):
        with patch.object(self.docs, "mark_index_event_done", side_effect=RuntimeError("crash")):
            self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder,
                                                max_events=1)["failed"], 1)
        self.assertEqual(self.sink.rows["1"]["event_id"], 1)
        self.assertEqual(self.docs.pending_index_events(limit=1)[0]["attempts"], 1)
        self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder,
                                            max_events=1),
                         {"processed": 1, "skipped_old": 1, "failed": 0})

    def test_publish_rolls_back_if_event_cannot_be_written(self):
        identifier = self.docs.create_upload(
            self.manager, title="审核资料", filename="review.txt", file_type="TXT",
            area="制度规范", visibility=Visibility.ORGANIZATION,
            content=b"review text", extracted_text="review text")
        event_count = len(self.docs.pending_index_events())
        epoch = self.docs._db.execute("SELECT version FROM search_policy_epoch").fetchone()[0]
        with patch.object(self.docs, "_enqueue_index_event", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                self.docs.publish(self.manager, identifier)
        state = self.docs._db.execute("SELECT state FROM documents WHERE document_id = ?",
                                      (identifier,)).fetchone()[0]
        self.assertEqual(state, "review")
        self.assertEqual(len(self.docs.pending_index_events()), event_count)
        self.assertEqual(self.docs._db.execute(
            "SELECT version FROM search_policy_epoch").fetchone()[0], epoch)

    def test_exhausted_event_can_be_superseded_by_rebuild(self):
        for _ in range(3):
            self.sink.fail_next = True
            self.assertEqual(drain_index_outbox(self.docs, self.sink, self.encoder,
                                                max_events=1)["failed"], 1)
        self.assertEqual(self.docs.pending_index_events(limit=1)[0]["event_id"], 2)
        self.assertEqual(self.docs.queue_index_rebuild(), 6)
        self.assertEqual(len(self.docs.pending_index_events()), 11)


if __name__ == "__main__":
    unittest.main()
