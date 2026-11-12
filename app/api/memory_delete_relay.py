"""SQLite deletion outbox relay for the PostgreSQL memory backend."""

import psycopg
import sqlite3
from contextlib import closing
from threading import Event
from typing import Callable

from app.api.conversations import ConversationRepository
from app.api.pg_conversations import PgConversationRepository
from app.api.pg_user_memories import PgUserMemoryRepository
from app.security.policy import Classification, Subject


def _deliver_claimed(conversations: ConversationRepository | PgConversationRepository, event: dict,
                     delete: Callable[[Subject, str], None]) -> str:
    subject = Subject(event["owner_id"], event["org_id"], frozenset(),
                      Classification.INTERNAL)
    try:
        # Idempotent: an already deleted source remains a tombstone.
        delete(subject, event["conversation_id"])
    except psycopg.OperationalError:
        conversations.retry_delete_event(event["id"], event["lease_token"],
                                         "target_unavailable")
        return "retry"
    except Exception:
        conversations.retry_delete_event(event["id"], event["lease_token"],
                                         "target_error")
        return "retry"
    return ("delivered" if conversations.ack_delete_event(
        event["id"], event["lease_token"]) else "lease_lost")


def relay_one_delete(conversations: ConversationRepository | PgConversationRepository,
                     memories: PgUserMemoryRepository) -> str:
    """At-least-once delivery with an existing PostgreSQL connection."""
    event = conversations.claim_delete_event()
    return ("idle" if event is None else _deliver_claimed(
        conversations, event, memories.discard_conversation_auto))


class MemoryDeleteRelayWorker:
    """Opt-in loop. Open a fresh target connection per event after failures."""

    def __init__(self, conversations: ConversationRepository | PgConversationRepository,
                 memory_factory: Callable[[], PgUserMemoryRepository],
                 *, poll_seconds: float = 1.0,
                 observe: Callable[[dict], None] | None = None):
        if poll_seconds <= 0:
            raise ValueError("INVALID_RELAY_POLL_INTERVAL")
        self.conversations = conversations
        self.memory_factory = memory_factory
        self.poll_seconds = poll_seconds
        self.observe = observe

    def run_once(self) -> str:
        event = self.conversations.claim_delete_event()
        if event is None:
            return "idle"

        def delete(subject: Subject, conversation_id: str) -> None:
            with closing(self.memory_factory()) as repository:
                repository.discard_conversation_auto(subject, conversation_id)

        return _deliver_claimed(self.conversations, event, delete)

    def run_until_stopped(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                status = self.run_once()
                snapshot = self.conversations.delete_outbox_metrics()
                if self.observe is not None:
                    self.observe({"status": status, **snapshot})
            except (sqlite3.OperationalError, psycopg.OperationalError):
                # The lease expires if the source store is temporarily unavailable.
                status = "source_unavailable"
            if status != "delivered":
                stop.wait(self.poll_seconds)
