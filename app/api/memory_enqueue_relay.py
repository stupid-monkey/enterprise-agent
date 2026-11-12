"""Reliable handoff of saved general turns to the PostgreSQL memory queue."""

import sqlite3
import psycopg
from threading import Event
from typing import Callable

from app.api.conversations import ConversationRepository
from app.api.pg_conversations import PgConversationRepository
from app.api.postgres_memory_service import PostgresMemoryService
from app.security.policy import Classification, Subject


class MemoryEnqueueRelayWorker:
    def __init__(self, conversations: ConversationRepository | PgConversationRepository,
                 memories: PostgresMemoryService,
                 *, poll_seconds: float = 1.0,
                 on_enqueued: Callable[[], None] | None = None):
        if poll_seconds <= 0:
            raise ValueError("INVALID_RELAY_POLL_INTERVAL")
        self.conversations = conversations
        self.memories = memories
        self.poll_seconds = poll_seconds
        self.on_enqueued = on_enqueued

    def run_once(self) -> str:
        event = self.conversations.claim_memory_event()
        if event is None:
            return "idle"
        if event["question"] is None:
            return ("discarded" if self.conversations.ack_memory_event(
                event["id"], event["lease_token"]) else "lease_lost")
        subject = Subject(event["owner_id"], event["org_id"], frozenset(),
                          Classification.INTERNAL)
        try:
            job_id = self.memories.enqueue_suggestion(
                subject, event["question"], event["conversation_id"],
                include_ordinary=bool(event["include_ordinary"]),
                context=event["context"], event_id=event["id"])
        except Exception:
            self.conversations.retry_memory_event(
                event["id"], event["lease_token"], "target_unavailable")
            return "retry"
        acknowledged = self.conversations.ack_memory_event(
            event["id"], event["lease_token"])
        if not acknowledged:
            return "lease_lost"
        if job_id is not None and self.on_enqueued is not None:
            self.on_enqueued()
        return "delivered" if job_id is not None else "discarded"

    def run_until_stopped(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                status = self.run_once()
            except (sqlite3.OperationalError, psycopg.OperationalError):
                status = "source_unavailable"
            if status != "delivered":
                stop.wait(self.poll_seconds)
