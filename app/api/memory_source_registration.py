"""Opt-in ownership gate for registering SQLite conversations in the PG lab.

The product API still uses SQLite memories. This adapter is for synthetic
migration rehearsals; it does not make two databases one transaction.
"""

from app.api.conversations import ConversationRepository
from app.api.pg_user_memories import PgUserMemoryRepository
from app.security.policy import Subject


def register_owned_source(subject: Subject, conversation_id: str,
                          conversations: ConversationRepository,
                          memories: PgUserMemoryRepository) -> bool:
    """Register only an owned source, then close a deletion race conservatively.

    The delete outbox is required so a deletion after the final ownership read
    still reaches PostgreSQL. A new source remains pending and cannot feed a
    worker until the second ownership check succeeds.
    """
    if not conversations.enable_delete_outbox:
        raise RuntimeError("CONVERSATION_DELETE_OUTBOX_REQUIRED")
    if not conversation_id or not conversations.owns(subject, conversation_id):
        return False
    if not memories.prepare_source(subject, conversation_id):
        return False
    try:
        still_owned = conversations.owns(subject, conversation_id)
    except Exception:
        memories.discard_conversation_auto(subject, conversation_id)
        raise
    if not still_owned:
        memories.discard_conversation_auto(subject, conversation_id)
        return False
    return memories.activate_source(subject, conversation_id)
