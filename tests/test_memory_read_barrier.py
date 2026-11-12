import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.api.conversations import ConversationRepository
from app.api.memory_read_barrier import list_memories_with_source_barrier
from app.security.policy import Classification, Subject


ALICE = Subject("alice", "org-a", frozenset(), Classification.INTERNAL)
BOB = Subject("bob", "org-a", frozenset(), Classification.INTERNAL)


class MemoryReadBarrierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conversations = ConversationRepository(Path(self.temp.name) / "conversations.sqlite3",
                                                    enable_delete_outbox=True)
        self.source = self.conversations.append(ALICE, None, "你好", "general", {"status": "ok"})
        self.manual = {"kind": "answer_preference", "value": "简短回答", "origin": "manual",
                       "source_conversation_id": None}
        self.auto = {"kind": "role", "value": "设计师", "origin": "auto",
                     "source_conversation_id": self.source}
        self.memories = Mock()
        self.memories.list_for.return_value = [self.manual, self.auto]

    def tearDown(self):
        self.conversations.close()
        self.temp.cleanup()

    def test_deleted_or_foreign_source_is_hidden_before_relay(self):
        self.assertEqual(list_memories_with_source_barrier(ALICE, self.memories,
                                                            self.conversations),
                         [self.manual, self.auto])
        self.assertEqual(list_memories_with_source_barrier(BOB, self.memories,
                                                            self.conversations), [self.manual])
        self.assertTrue(self.conversations.delete(ALICE, self.source))
        self.assertEqual(self.conversations.pending_delete_count(), 1)
        self.assertEqual(list_memories_with_source_barrier(ALICE, self.memories,
                                                            self.conversations), [self.manual])

    def test_merge_removes_old_source_visibility(self):
        target = self.conversations.append(ALICE, None, "继续", "general", {"status": "ok"})
        self.assertTrue(self.conversations.merge(ALICE, target, self.source))
        self.assertEqual(list_memories_with_source_barrier(ALICE, self.memories,
                                                            self.conversations), [self.manual])

    def test_source_read_error_fails_closed(self):
        with patch.object(self.conversations, "owned_source_ids", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                list_memories_with_source_barrier(ALICE, self.memories, self.conversations)


if __name__ == "__main__":
    unittest.main()
