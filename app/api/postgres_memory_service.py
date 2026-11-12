"""Product-facing PostgreSQL memory adapter with SQLite source authority.

Each request/worker thread owns a separate psycopg connection. Every automatic
fact and candidate read is checked against the authoritative conversation DB.
The SQLite delete outbox must be enabled before this adapter can be used.
"""

import threading

import psycopg

from app.api.conversations import ConversationRepository
from app.api.pg_conversations import PgConversationRepository
from app.api.memory_read_barrier import list_memories_with_source_barrier
from app.api.memory_source_registration import register_owned_source
from app.api.pg_user_memories import PgUserMemoryRepository
from app.security.policy import Subject


class PostgresMemoryService:
    def __init__(self, dsn: str, conversations: ConversationRepository | PgConversationRepository, *,
                 schema: str = "memory", default_auto_suggest: bool = False,
                 create_schema: bool = False):
        if not conversations.enable_delete_outbox:
            raise RuntimeError("CONVERSATION_DELETE_OUTBOX_REQUIRED")
        if not dsn:
            raise ValueError("MEMORY_DATABASE_URL_REQUIRED")
        self.dsn = dsn
        self.schema = schema
        self.conversations = conversations
        self.default_auto_suggest = default_auto_suggest
        self._local = threading.local()
        self._lock = threading.Lock()
        self._repositories: list[PgUserMemoryRepository] = []
        self._closed = False
        first = self._repository()
        if create_schema:
            first.create_schema()
        else:
            first.check_schema()

    def _repository(self) -> PgUserMemoryRepository:
        repository = getattr(self._local, "repository", None)
        if repository is not None and not repository.connection.closed:
            return repository
        with self._lock:
            if self._closed:
                raise RuntimeError("POSTGRES_MEMORY_SERVICE_CLOSED")
            if repository is not None:
                repository.close()
                self._repositories = [item for item in self._repositories
                                      if item is not repository]
            repository = PgUserMemoryRepository(
                self.dsn, schema=self.schema,
                default_auto_suggest=self.default_auto_suggest)
            self._repositories.append(repository)
            self._local.repository = repository
            return repository

    def __getattr__(self, name: str):
        return getattr(self._repository(), name)

    def list_for(self, subject: Subject) -> list[dict]:
        return list_memories_with_source_barrier(
            subject, self._repository(), self.conversations)

    def list_suggestions(self, subject: Subject) -> list[dict]:
        suggestions = self._repository().list_suggestions(subject)
        sources = {item["source_conversation_id"] for item in suggestions}
        live = self.conversations.owned_source_ids(subject, sources)
        return [item for item in suggestions if item["source_conversation_id"] in live]

    def accept_suggestion(self, subject: Subject, suggestion_id: str,
                          value: str) -> dict | None:
        suggestion = next((item for item in self.list_suggestions(subject)
                           if item["id"] == suggestion_id), None)
        if suggestion is None:
            return None
        # A read-only liveness check would race with deletion on another process.
        # Hold the SQLite source until the PostgreSQL transaction commits.
        with self.conversations.hold_owned_source(
                subject, suggestion["source_conversation_id"]) as owned:
            if not owned:
                return None
            return self._repository().accept_suggestion(subject, suggestion_id, value)

    def reject_suggestion(self, subject: Subject, suggestion_id: str) -> bool:
        suggestion = next((item for item in self.list_suggestions(subject)
                           if item["id"] == suggestion_id), None)
        if suggestion is None:
            return False
        with self.conversations.hold_owned_source(
                subject, suggestion["source_conversation_id"]) as owned:
            return owned and self._repository().reject_suggestion(subject, suggestion_id)

    def enqueue_suggestion(self, subject: Subject, question: str,
                           conversation_id: str, *, include_ordinary: bool = True,
                           context: tuple[str, ...] = (),
                           event_id: str | None = None) -> str | None:
        repository = self._repository()
        if not register_owned_source(subject, conversation_id,
                                     self.conversations, repository):
            return None
        job_id = repository.enqueue_suggestion(subject, question, conversation_id,
                                               context, include_ordinary=include_ordinary,
                                               event_id=event_id)
        if not self.conversations.owns(subject, conversation_id):
            repository.discard_conversation_suggestions(subject, conversation_id)
            return None
        return job_id

    def healthy(self) -> bool:
        repository = self._repository()
        try:
            repository.connection.execute("SELECT 1")
        except psycopg.Error:
            # A PostgreSQL restart can leave a thread-local connection broken
            # while `closed` is still false. Rebuild it before reporting ready.
            repository.close()
            try:
                self._repository().connection.execute("SELECT 1")
            except Exception as exc:
                raise RuntimeError("MEMORY_PG_RECONNECT_FAILED") from exc
        try:
            self.conversations.delete_outbox_metrics()
        except Exception as exc:
            raise RuntimeError("MEMORY_SQLITE_METRICS_FAILED") from exc
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for repository in self._repositories:
                repository.close()
            self._repositories.clear()
