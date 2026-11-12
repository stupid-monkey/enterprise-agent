"""Opt-in source liveness check before exposing experimental PostgreSQL memory.

The production API still reads SQLite memories. A future PostgreSQL reader must
use this adapter while conversations remain authoritative in SQLite.
"""

from app.api.conversations import ConversationRepository
from app.api.pg_user_memories import PgUserMemoryRepository
from app.security.policy import Subject


def list_memories_with_source_barrier(subject: Subject,
                                      memories: PgUserMemoryRepository,
                                      conversations: ConversationRepository) -> list[dict]:
    """Hide auto facts whose owner-scoped source no longer exists in SQLite.

    A deletion committed before the SQLite liveness read cannot expose its
    stale PostgreSQL fact, even when the delete relay has not run yet. A source
    read failure propagates; callers must not fall back to the raw PG result.
    """
    facts = memories.list_for(subject)
    sources = {fact["source_conversation_id"] for fact in facts
               if fact["origin"] == "auto" and fact.get("source_conversation_id")}
    live = conversations.owned_source_ids(subject, sources)
    return [fact for fact in facts if fact["origin"] != "auto"
            or fact.get("source_conversation_id") in live]
