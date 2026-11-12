"""Minimal authenticated service boundary. No legacy Chroma query is exposed."""

import os
import json
import time
import threading
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, RateLimitError

from app.api.auth import LocalAuth, account_from_environment
from app.api.answer_style import classify_answer_style, format_grounded_paragraphs
from app.api.audit import AuditRepository
from app.api.artifacts import (ArtifactRepository, CompositionConflict, CompositionNoEvidence,
                               DraftChanged, EditSourceTooLarge, InvalidEdit, InvalidNote, InvalidReview,
                               ReviewRequired, SourceVersionChanged)
from app.api.collaboration import CollaborationInvalid, CollaborationModel, OpenAICollaborationModel
from app.api.collaboration_runs import CollaborationRunRepository
from app.api.slide_visuals import UnsplashPhotoProvider
from app.api.compound_tasks import compose_and_collaborate
from app.api.content import MAX_UPLOAD_BYTES, extract_text, relevant_sentences, search_terms
from app.api.conditional_answers import (ConditionEvidence, ConditionResolver, OpenAIConditionResolver,
                                         local_year_entitlement, relevant_condition, validate_decision)
from app.api.conversations import ConversationRepository
from app.api.pg_conversations import PgConversationRepository
from app.api.documents import DocumentRecord, DocumentRepository, OriginalVersionChanged
from app.api.search_cache import RedisSearchCache
from app.api.gpt_answers import OpenAIQuoteSelector, QuoteSelector, validate_selection
from app.api.general_chat import GeneralResponder, OpenAIGeneralResponder, local_reply
from app.api.self_identity import account_question_kind, asks_own_account, own_account_answer
from app.api.file_access_questions import (FileAccessQuery, answer_file_access,
                                           model_permission_fallback_allowed, parse_file_access_query)
from app.api.intent import (high_confidence_intent, knowledge_query,
                            needs_deliverable_classification, route_file_intent, route_intent)
from app.api.intent_model import (IntentClassifier, IntentDecision, OpenAIIntentClassifier,
                                  focus_supported, retrieval_query)
from app.api.memory_suggestions import (MemorySuggester, OpenAIMemorySuggester,
                                        eligible_auto_memory_text, eligible_user_text)
from app.api.memory_delete_relay import MemoryDeleteRelayWorker
from app.api.memory_enqueue_relay import MemoryEnqueueRelayWorker
from app.api.pg_user_memories import PgUserMemoryRepository
from app.api.postgres_memory_service import PostgresMemoryService
from app.api.meeting_rooms import MeetingRoomRepository
from app.api.meeting_minutes import MinutesInvalid, MinutesModel, OpenAIMinutesModel, validate_minutes
from app.api.user_memories import MEMORY_KINDS, UserMemoryRepository
from app.api.workflow_answers import grounded_extract, run_knowledge_workflow
from app.api.workflow_runs import WorkflowRunRepository
from app.agent.react import OpenAIReactModel, ReactLimits, ReactModel, run_react
from app.agent.reflection import OpenAIReflectionModel, ReflectionInvalid, ReflectionModel
from app.security.policy import Classification, ResourceAction, Subject, UserRole, Visibility
from app.workflow.graph import WorkResult


logger = logging.getLogger(__name__)


def memory_failure_code(error: Exception) -> tuple[str, bool]:
    """Classify errors without recording model input or exception messages."""
    if isinstance(error, (APITimeoutError, TimeoutError)):
        return "model_timeout", True
    if isinstance(error, (RateLimitError,)):
        return "model_rate_limited", True
    if isinstance(error, (APIConnectionError, ConnectionError)):
        return "model_unavailable", True
    if isinstance(error, AuthenticationError):
        return "model_auth_failed", False
    if isinstance(error, APIStatusError):
        return ("model_unavailable", True) if error.status_code >= 500 else ("model_invalid", False)
    if isinstance(error, (ValueError, TypeError)):
        return "model_invalid", False
    return "memory_write_failed", False


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    user_id: str
    org_id: str
    department_ids: list[str]
    clearance: str
    role: UserRole


class MemoryInput(BaseModel):
    value: str = Field(min_length=1, max_length=160)


class MemoryResponse(BaseModel):
    kind: Literal["role", "task", "answer_preference"]
    value: str
    created_at: str
    updated_at: str
    origin: str = "manual"
    source_conversation_id: str | None = None
    expires_at: str | None = None


class MemorySettingsInput(BaseModel):
    auto_suggest: bool


class MemorySuggestionResponse(BaseModel):
    id: str
    kind: Literal["role", "task", "answer_preference"]
    value: str
    source_conversation_id: str
    source_text: str
    created_at: str


class MemorySuggestionFailureResponse(BaseModel):
    job_id: str
    reason_code: str
    attempts: int
    failed_at: str


class AccountResponse(MeResponse):
    username: str


class DocumentSummary(BaseModel):
    id: str
    title: str
    type: str
    area: str
    visibility: str
    version: str
    date: str
    owner: str
    has_original: bool


class DocumentDetail(DocumentSummary):
    page: str
    excerpt: str


class DocumentPreview(DocumentDetail):
    preview_text: str
    truncated: bool


class AccessChange(BaseModel):
    user_id: str = Field(min_length=1)
    denied: bool


class QuestionRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    area: str | None = None


class Citation(BaseModel):
    document_id: str
    title: str
    version: str
    location: str
    excerpt: str | None = None


class AnswerResponse(BaseModel):
    status: str
    paragraphs: list[str]
    citations: list[Citation]
    document_requested: bool = False
    edit: dict | None = None
    target_artifact_id: str | None = None
    target_artifact_token: str | None = None
    target_document_id: str | None = None
    target_document_version: str | None = None
    clarification: dict | None = None


class ReactResponse(AnswerResponse):
    trace: list[dict]
    model_calls: int
    tool_calls: int
    error_code: str | None = None


class ConversationQuestion(QuestionRequest):
    conversation_id: str | None = None
    mode: str = "extract"
    workflow_run_id: str | None = None
    artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_document_id: str | None = Field(default=None, min_length=1, max_length=80)


class ConversationAnswer(BaseModel):
    conversation_id: str
    answer: AnswerResponse


class ConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: str
    turn_count: int


class ConversationTurn(BaseModel):
    question: str
    mode: str
    answer: AnswerResponse


class ConversationDetail(BaseModel):
    id: str
    title: str
    updated_at: str
    turns: list[ConversationTurn]


class ArtifactEditRequest(BaseModel):
    artifact_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_document_id: str | None = Field(default=None, min_length=1, max_length=80)
    instruction: str = Field(min_length=2, max_length=1000)
    conversation_id: str | None = None


class MeetingRoomInput(BaseModel):
    department_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=2, max_length=80)


class MeetingMemberInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)


class MeetingMessageInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AuditEventResponse(BaseModel):
    id: int
    occurred_at: str
    org_id: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    trace_id: str


class ArtifactRequest(BaseModel):
    kind: str
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(max_length=1000)
    source_ids: list[str] = Field(min_length=1, max_length=10)
    outline: list[str] = Field(min_length=1, max_length=10)
    theme: str = "brief"
    mode: str = "standard"


class SlideOutlineRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=10)
    count: int = Field(ge=1, le=10)


class ComposeFromQuestionRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    request_id: str = Field(min_length=8, max_length=80)


class ComposeFromAnswerRequest(ComposeFromQuestionRequest):
    conversation_id: str | None = Field(default=None, min_length=1, max_length=80)
    turn_index: int | None = Field(default=None, ge=0, le=99)


class ConfirmRequest(BaseModel):
    outline: list[str] = Field(min_length=1, max_length=10)
    draft_token: str | None = Field(default=None, min_length=64, max_length=64)


class ComparisonReview(BaseModel):
    heading: str = Field(min_length=1, max_length=100)
    decision: Literal["aligned", "different", "unresolved"]
    note: str = Field(max_length=500)


class ReviewRequest(BaseModel):
    reviews: list[ComparisonReview] = Field(min_length=1, max_length=10)


class SectionNote(BaseModel):
    heading: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=1200)


class NotesRequest(BaseModel):
    draft_token: str = Field(min_length=64, max_length=64)
    notes: list[SectionNote] = Field(min_length=1, max_length=10)


class CollaborationRequest(BaseModel):
    draft_token: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=8, max_length=80)


class SlideVisualRequest(BaseModel):
    draft_token: str = Field(min_length=64, max_length=64)


class ReflectionRequest(BaseModel):
    draft_token: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=8, max_length=80)


class WorkflowRunResponse(BaseModel):
    id: str
    status: str


class CollaborationRunResponse(BaseModel):
    id: str
    request_id: str
    draft_token: str
    status: str
    stage: str | None
    error_code: str | None
    trace: list[dict]
    attempts: int
    max_attempts: int
    can_retry: bool


class CompoundTaskResponse(BaseModel):
    artifact: dict
    run: CollaborationRunResponse | None
    status: Literal["completed", "failed", "pending", "not_run"]


def document_summary(record: DocumentRecord) -> DocumentSummary:
    visibility = {
        "organization": "内部公开", "department": "部门受限", "allowlist": "机密",
    }[record.policy.visibility.value]
    return DocumentSummary(
        id=record.document_id, title=record.title, type=record.file_type,
        area=record.area, visibility=visibility, version=record.version,
        date=record.updated_at, owner=record.owner, has_original=record.has_original,
    )


def create_app(auth: LocalAuth | None = None, documents: DocumentRepository | None = None,
               artifacts: ArtifactRepository | None = None, quote_selector: QuoteSelector | None = None,
               audit: AuditRepository | None = None,
               conversations: ConversationRepository | PgConversationRepository | None = None,
               general_responder: GeneralResponder | None = None,
               intent_classifier: IntentClassifier | None = None,
               auto_memory: bool | None = None,
               workflow_runs: WorkflowRunRepository | None = None,
               memories: UserMemoryRepository | PostgresMemoryService | None = None,
               memory_suggester: MemorySuggester | None = None,
               collaboration_model: CollaborationModel | None = None,
               collaboration_runs: CollaborationRunRepository | None = None,
               react_model: ReactModel | None = None,
               reflection_model: ReflectionModel | None = None,
               meeting_rooms: MeetingRoomRepository | None = None,
               minutes_model: MinutesModel | None = None,
               slide_photo_provider: UnsplashPhotoProvider | None = None,
               condition_resolver: ConditionResolver | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(current_app: FastAPI):
        memory_worker_stop.clear()
        current_app.state.memories.recover_suggestion_jobs()
        start_memory_worker()
        relay_stop = threading.Event()
        relay_thread = None
        enqueue_thread = None
        if current_app.state.postgres_memory_enabled:
            memory_service = current_app.state.memories
            relay = MemoryDeleteRelayWorker(
                current_app.state.conversations,
                lambda: PgUserMemoryRepository(memory_service.dsn,
                                               schema=memory_service.schema),
                poll_seconds=1.0)
            relay_thread = threading.Thread(target=relay.run_until_stopped,
                                            args=(relay_stop,), daemon=True,
                                            name="memory-delete-relay")
            relay_thread.start()
            enqueue_relay = MemoryEnqueueRelayWorker(
                current_app.state.conversations, memory_service,
                poll_seconds=1.0, on_enqueued=start_memory_worker)
            enqueue_thread = threading.Thread(target=enqueue_relay.run_until_stopped,
                                              args=(relay_stop,), daemon=True,
                                              name="memory-enqueue-relay")
            enqueue_thread.start()
        current_app.state.memory_relay_thread = relay_thread
        current_app.state.memory_enqueue_thread = enqueue_thread
        try:
            yield
        finally:
            relay_stop.set()
            memory_worker_stop.set()
            if relay_thread is not None:
                relay_thread.join(timeout=5)
            if enqueue_thread is not None:
                enqueue_thread.join(timeout=5)
            if memory_worker_thread is not None:
                memory_worker_thread.join(timeout=5)

    app = FastAPI(title="Enterprise Agent API", version="0.1.0", lifespan=lifespan)
    app.state.auth = auth if auth is not None else LocalAuth(account_from_environment())
    local_demo = os.getenv("APP_ENV", "").lower() == "local" and os.getenv("ENABLE_DEV_ACCOUNTS", "").lower() == "true"
    catalog_name = "demo_documents.sqlite3" if local_demo else "documents.sqlite3"
    catalog_path = Path(__file__).resolve().parents[2] / "data" / catalog_name if auth is None else ":memory:"
    cache_url = os.getenv("REDIS_CACHE_URL", "").strip() if auth is None else ""
    search_cache = RedisSearchCache(cache_url) if cache_url else None
    shadow_outbox = (os.getenv("SHADOW_INDEX_OUTBOX", "").lower() == "true"
                     if auth is None else False)
    app.state.documents = (documents if documents is not None else
                           DocumentRepository(catalog_path, seed_demo=local_demo and auth is None,
                                              search_cache=search_cache,
                                              record_index_events=shadow_outbox))
    meeting_path = Path(__file__).resolve().parents[2] / "data" / ("demo_meeting_rooms.sqlite3" if local_demo else "meeting_rooms.sqlite3") if auth is None else ":memory:"
    app.state.meeting_rooms = meeting_rooms if meeting_rooms is not None else MeetingRoomRepository(meeting_path)
    artifacts_name = "demo_artifacts.sqlite3" if local_demo else "artifacts.sqlite3"
    artifacts_path = Path(__file__).resolve().parents[2] / "data" / artifacts_name if auth is None else ":memory:"
    app.state.artifacts = artifacts if artifacts is not None else ArtifactRepository(artifacts_path)
    photo_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip() if auth is None else ""
    app.state.slide_photo_provider = slide_photo_provider or (UnsplashPhotoProvider(photo_key) if photo_key else None)
    audit_path = Path(__file__).resolve().parents[2] / "data" / ("demo_audit.sqlite3" if local_demo else "audit.sqlite3") if auth is None else ":memory:"
    app.state.audit = audit if audit is not None else AuditRepository(audit_path)
    conversation_path = Path(__file__).resolve().parents[2] / "data" / ("demo_conversations.sqlite3" if local_demo else "conversations.sqlite3") if auth is None else ":memory:"
    memory_backend = (os.getenv("MEMORY_BACKEND", "sqlite").strip().lower()
                      if memories is None else
                      ("postgres" if isinstance(memories, PostgresMemoryService) else "sqlite"))
    if memory_backend not in ("sqlite", "postgres"):
        raise ValueError("INVALID_MEMORY_BACKEND")
    conversation_backend = (os.getenv("CONVERSATION_BACKEND", "sqlite").strip().lower()
                            if conversations is None else
                            ("postgres" if isinstance(conversations, PgConversationRepository) else "sqlite"))
    if conversation_backend not in ("sqlite", "postgres"):
        raise ValueError("INVALID_CONVERSATION_BACKEND")
    if conversation_backend == "postgres" and memory_backend != "postgres" and conversations is None:
        raise ValueError("POSTGRES_CONVERSATIONS_REQUIRE_POSTGRES_MEMORY")
    if conversations is not None:
        app.state.conversations = conversations
    elif conversation_backend == "postgres":
        app.state.conversations = PgConversationRepository(
            os.getenv("MEMORY_DATABASE_URL", ""),
            schema=os.getenv("MEMORY_SCHEMA", "memory"))
    else:
        app.state.conversations = ConversationRepository(
            conversation_path, enable_delete_outbox=memory_backend == "postgres")
    app.state.postgres_conversations_enabled = conversation_backend == "postgres"
    memories_path = Path(__file__).resolve().parents[2] / "data" / ("demo_user_memories.sqlite3" if local_demo else "user_memories.sqlite3") if auth is None else ":memory:"
    app.state.auto_memory_enabled = (auto_memory if auto_memory is not None else
                                     auth is None and os.getenv("AUTO_MEMORY", "true").lower() != "false")
    if memories is not None:
        app.state.memories = memories
    elif memory_backend == "postgres":
        service = PostgresMemoryService(
            os.getenv("MEMORY_DATABASE_URL", ""), app.state.conversations,
            schema=os.getenv("MEMORY_SCHEMA", "memory"),
            default_auto_suggest=app.state.auto_memory_enabled)
        try:
            service.verify_import_manifest(
                os.getenv("MEMORY_CUTOVER_MEMORY_SHA256", ""),
                os.getenv("MEMORY_CUTOVER_CONVERSATION_SHA256", ""))
        except Exception:
            service.close()
            raise
        app.state.memories = service
    else:
        app.state.memories = UserMemoryRepository(
            memories_path, default_auto_suggest=app.state.auto_memory_enabled)
    app.state.postgres_memory_enabled = memory_backend == "postgres"
    app.state.memory_relay_thread = None
    app.state.memory_enqueue_thread = None
    app.state.memory_suggester = memory_suggester
    memory_worker_lock = threading.Lock()
    memory_worker_running = False
    memory_worker_stop = threading.Event()
    memory_worker_thread = None
    runs_path = Path(__file__).resolve().parents[2] / "data" / ("demo_workflow_runs.sqlite3" if local_demo else "workflow_runs.sqlite3") if auth is None else ":memory:"
    app.state.workflow_runs = workflow_runs
    workflow_runs_lock = threading.Lock()
    collaboration_runs_path = (Path(__file__).resolve().parents[2] / "data" /
                               ("demo_collaboration_runs.sqlite3" if local_demo else "collaboration_runs.sqlite3")
                               if auth is None else ":memory:")
    app.state.collaboration_runs = collaboration_runs
    collaboration_runs_lock = threading.Lock()

    def collaboration_runs_repository() -> CollaborationRunRepository:
        if app.state.collaboration_runs is None:
            with collaboration_runs_lock:
                if app.state.collaboration_runs is None:
                    app.state.collaboration_runs = CollaborationRunRepository(collaboration_runs_path)
        return app.state.collaboration_runs

    def runs_repository() -> WorkflowRunRepository:
        if app.state.workflow_runs is None:
            with workflow_runs_lock:
                if app.state.workflow_runs is None:
                    app.state.workflow_runs = WorkflowRunRepository(runs_path)
        return app.state.workflow_runs
    gpt_configured = os.getenv("ENABLE_GPT_ANSWERS", "").lower() == "true" and bool(os.getenv("OPENAI_API_KEY", "").strip())
    app.state.gpt_enabled = quote_selector is not None or gpt_configured
    app.state.collaboration_model = collaboration_model
    app.state.collaboration_enabled = collaboration_model is not None or bool(os.getenv("OPENAI_API_KEY", "").strip())
    app.state.react_model = react_model
    app.state.react_enabled = react_model is not None or bool(os.getenv("OPENAI_API_KEY", "").strip())
    app.state.auto_react_enabled = (
        app.state.react_enabled
        and (auth is None or react_model is not None)
        and os.getenv("AUTO_REACT", "true").lower() != "false"
    )
    app.state.reflection_model = reflection_model
    app.state.reflection_enabled = reflection_model is not None or bool(os.getenv("OPENAI_API_KEY", "").strip())
    # Real deployments auto-run the internal reflection step; injected in-memory test apps opt in explicitly.
    app.state.auto_reflection_enabled = (
        app.state.reflection_enabled
        and (auth is None or reflection_model is not None)
        and os.getenv("AUTO_REFLECTION", "true").lower() != "false"
    )
    app.state.quote_selector = quote_selector
    app.state.general_responder = general_responder
    app.state.general_enabled = general_responder is not None or bool(os.getenv("OPENAI_API_KEY", "").strip())
    app.state.intent_classifier = intent_classifier
    app.state.ai_intent_enabled = (intent_classifier is not None or
        (auth is None and bool(os.getenv("OPENAI_API_KEY", "").strip())
         and os.getenv("ENABLE_AI_INTENT_ROUTER", "true").lower() != "false"))
    app.state.minutes_model = minutes_model

    @app.middleware("http")
    async def add_trace_id(request: Request, call_next):
        trace_id = uuid4().hex
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": "error", "code": exc.detail, "trace_id": request.state.trace_id},
            headers={"X-Trace-Id": request.state.trace_id},
        )

    def current_token(request: Request) -> str:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
        return token

    def current_subject(request: Request, token: str = Depends(current_token)) -> Subject:
        subject = request.app.state.auth.resolve(token)
        if subject is None:
            raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
        return subject

    def record_event(request: Request, subject: Subject, action: str, resource_type: str,
                     resource_id: str, outcome: str) -> None:
        request.app.state.audit.record(
            org_id=subject.org_id, actor_id=subject.user_id, action=action,
            resource_type=resource_type, resource_id=resource_id, outcome=outcome,
            trace_id=request.state.trace_id,
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request):
        if not request.app.state.auth.configured:
            raise HTTPException(status_code=503, detail="AUTH_NOT_CONFIGURED")
        if request.app.state.postgres_conversations_enabled:
            try:
                request.app.state.conversations.healthy()
            except Exception as exc:
                raise HTTPException(status_code=503, detail="CONVERSATION_BACKEND_UNAVAILABLE") from exc
        if request.app.state.postgres_memory_enabled:
            try:
                request.app.state.memories.healthy()
            except Exception as exc:
                logger.warning("Memory readiness failure type=%s cause_type=%s",
                               type(exc).__name__,
                               type(exc.__cause__).__name__ if exc.__cause__ else "none")
                raise HTTPException(status_code=503, detail="MEMORY_BACKEND_UNAVAILABLE") from exc
            relay_thread = request.app.state.memory_relay_thread
            if relay_thread is not None and not relay_thread.is_alive():
                raise HTTPException(status_code=503, detail="MEMORY_DELETE_RELAY_UNAVAILABLE")
            enqueue_thread = request.app.state.memory_enqueue_thread
            if enqueue_thread is not None and not enqueue_thread.is_alive():
                raise HTTPException(status_code=503, detail="MEMORY_ENQUEUE_RELAY_UNAVAILABLE")
        return {"status": "ready"}

    @app.get("/api/v1/admin/memory-status")
    def memory_status(request: Request,
                      subject: Subject = Depends(current_subject)):
        if subject.role != UserRole.SYSTEM_ADMIN:
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        if not request.app.state.postgres_memory_enabled:
            return {"backend": "sqlite", "queued_jobs": request.app.state.memories.suggestion_job_count()}
        return {
            "backend": "postgres",
            "queued_jobs": request.app.state.memories.suggestion_job_count(),
            "pending_enqueue_events": request.app.state.conversations.pending_memory_event_count(),
            "delete_outbox": request.app.state.conversations.delete_outbox_metrics(),
            "enqueue_relay_alive": bool(request.app.state.memory_enqueue_thread and
                                        request.app.state.memory_enqueue_thread.is_alive()),
            "delete_relay_alive": bool(request.app.state.memory_relay_thread and
                                       request.app.state.memory_relay_thread.is_alive()),
        }

    @app.post("/api/v1/auth/login", response_model=TokenResponse)
    def login(body: LoginRequest, request: Request):
        token = request.app.state.auth.login(body.username, body.password)
        if token is None:
            raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS")
        subject = request.app.state.auth.resolve(token)
        record_event(request, subject, "auth.login", "session", subject.user_id, "success")
        return TokenResponse(access_token=token)

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request, subject: Subject = Depends(current_subject), token: str = Depends(current_token)):
        request.app.state.auth.logout(token)
        record_event(request, subject, "auth.logout", "session", subject.user_id, "success")

    @app.get("/api/v1/me", response_model=MeResponse)
    def me(subject: Subject = Depends(current_subject)):
        return MeResponse(
            user_id=subject.user_id,
            org_id=subject.org_id,
            department_ids=sorted(subject.department_ids),
            clearance=subject.clearance.name,
            role=subject.role,
        )

    @app.get("/api/v1/me/memories", response_model=list[MemoryResponse])
    def list_memories(request: Request, response: Response,
                      subject: Subject = Depends(current_subject)):
        response.headers["Cache-Control"] = "no-store"
        return request.app.state.memories.list_for(subject)

    @app.get("/api/v1/me/memory-settings")
    def memory_settings(request: Request, response: Response,
                        subject: Subject = Depends(current_subject)):
        response.headers["Cache-Control"] = "no-store"
        return {"auto_suggest": request.app.state.memories.auto_suggest_enabled(subject)}

    @app.put("/api/v1/me/memory-settings")
    def update_memory_settings(body: MemorySettingsInput, request: Request, response: Response,
                               subject: Subject = Depends(current_subject)):
        request.app.state.memories.set_auto_suggest(subject, body.auto_suggest)
        record_event(request, subject, "memory.settings", "memory", "auto_suggest", "success")
        response.headers["Cache-Control"] = "no-store"
        return {"auto_suggest": body.auto_suggest}

    @app.get("/api/v1/me/memory-suggestions", response_model=list[MemorySuggestionResponse])
    def list_memory_suggestions(request: Request, response: Response,
                                subject: Subject = Depends(current_subject)):
        response.headers["Cache-Control"] = "no-store"
        return request.app.state.memories.list_suggestions(subject)

    @app.get("/api/v1/me/memory-suggestion-failures",
             response_model=list[MemorySuggestionFailureResponse])
    def list_memory_suggestion_failures(request: Request, response: Response,
                                        subject: Subject = Depends(current_subject)):
        response.headers["Cache-Control"] = "no-store"
        return request.app.state.memories.list_suggestion_failures(subject)

    @app.post("/api/v1/me/memory-suggestions/{suggestion_id}/accept", response_model=MemoryResponse)
    def accept_memory_suggestion(suggestion_id: str, body: MemoryInput, request: Request,
                                 response: Response, subject: Subject = Depends(current_subject)):
        value = body.value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HTTPException(status_code=422, detail="INVALID_MEMORY_VALUE")
        item = request.app.state.memories.accept_suggestion(subject, suggestion_id, value)
        if item is None:
            raise HTTPException(status_code=404, detail="MEMORY_SUGGESTION_NOT_FOUND")
        record_event(request, subject, "memory.suggestion.accept", "memory", item["kind"], "success")
        response.headers["Cache-Control"] = "no-store"
        return item

    @app.delete("/api/v1/me/memory-suggestions/{suggestion_id}", status_code=204)
    def reject_memory_suggestion(suggestion_id: str, request: Request,
                                 subject: Subject = Depends(current_subject)):
        if not request.app.state.memories.reject_suggestion(subject, suggestion_id):
            raise HTTPException(status_code=404, detail="MEMORY_SUGGESTION_NOT_FOUND")
        record_event(request, subject, "memory.suggestion.reject", "memory", "suggestion", "success")

    @app.put("/api/v1/me/memories/{kind}", response_model=MemoryResponse)
    def save_memory(kind: str, body: MemoryInput, request: Request, response: Response,
                    subject: Subject = Depends(current_subject)):
        if kind not in MEMORY_KINDS:
            raise HTTPException(status_code=422, detail="INVALID_MEMORY_KIND")
        value = body.value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise HTTPException(status_code=422, detail="INVALID_MEMORY_VALUE")
        item = request.app.state.memories.upsert(subject, kind, value)
        record_event(request, subject, "memory.upsert", "memory", kind, "success")
        response.headers["Cache-Control"] = "no-store"
        return item

    @app.delete("/api/v1/me/memories/{kind}", status_code=204)
    def delete_memory(kind: str, request: Request, subject: Subject = Depends(current_subject)):
        if kind not in MEMORY_KINDS:
            raise HTTPException(status_code=422, detail="INVALID_MEMORY_KIND")
        request.app.state.memories.delete(subject, kind)
        record_event(request, subject, "memory.delete", "memory", kind, "success")

    @app.get("/api/v1/capabilities")
    def capabilities(subject: Subject = Depends(current_subject)):
        return {"gpt_quotes": app.state.gpt_enabled,
                "collaboration": app.state.collaboration_enabled,
                "online_slide_images": app.state.slide_photo_provider is not None,
                "react": app.state.react_enabled,
                "reflection": app.state.reflection_enabled}

    @app.get("/api/v1/admin/accounts", response_model=list[AccountResponse])
    def list_accounts(request: Request, subject: Subject = Depends(current_subject)):
        if subject.role != UserRole.SYSTEM_ADMIN:
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        return [
            AccountResponse(
                username=username,
                user_id=account.user_id,
                org_id=account.org_id,
                department_ids=sorted(account.department_ids),
                clearance=account.clearance.name,
                role=account.role,
            )
            for username, account in request.app.state.auth.list_accounts()
            if account.org_id == subject.org_id
        ]

    @app.get("/api/v1/admin/audit-events", response_model=list[AuditEventResponse])
    def list_audit_events(request: Request, subject: Subject = Depends(current_subject)):
        if subject.role != UserRole.SYSTEM_ADMIN:
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        return request.app.state.audit.list_for_org(subject.org_id)

    @app.get("/api/v1/meeting-rooms")
    def list_meeting_rooms(request: Request, subject: Subject = Depends(current_subject)):
        return request.app.state.meeting_rooms.list_for(subject)

    @app.get("/api/v1/meeting-rooms/candidates")
    def meeting_candidates(request: Request, subject: Subject = Depends(current_subject)):
        if subject.role not in (UserRole.DEPARTMENT_MANAGER, UserRole.KNOWLEDGE_ADMIN, UserRole.SYSTEM_ADMIN):
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        return [{"user_id": account.user_id, "username": name, "role": account.role,
                 "department_ids": sorted(account.department_ids)}
                for name, account in request.app.state.auth.list_accounts()
                if account.org_id == subject.org_id and account.active]

    @app.post("/api/v1/meeting-rooms", status_code=201)
    def create_meeting_room(body: MeetingRoomInput, request: Request,
                            subject: Subject = Depends(current_subject)):
        if (subject.role not in (UserRole.DEPARTMENT_MANAGER, UserRole.KNOWLEDGE_ADMIN, UserRole.SYSTEM_ADMIN)
                or body.department_id not in subject.department_ids):
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        room = request.app.state.meeting_rooms.create(subject, body.department_id, body.title.strip())
        if room is None:
            raise HTTPException(status_code=409, detail="ROOM_ALREADY_EXISTS")
        record_event(request, subject, "meeting.create", "meeting_room", room["id"], "success")
        return room

    @app.get("/api/v1/meeting-rooms/{room_id}")
    def get_meeting_room(room_id: str, request: Request,
                         subject: Subject = Depends(current_subject)):
        room = request.app.state.meeting_rooms.get(subject, room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="ROOM_NOT_FOUND")
        names = {account.user_id: name for name, account in request.app.state.auth.list_accounts()
                 if account.org_id == subject.org_id}
        room["members"] = [member | {"username": names.get(member["user_id"], member["user_id"])}
                           for member in room["members"]]
        return room

    @app.post("/api/v1/meeting-rooms/{room_id}/members")
    def add_meeting_member(room_id: str, body: MeetingMemberInput, request: Request,
                           subject: Subject = Depends(current_subject)):
        room = request.app.state.meeting_rooms.get(subject, room_id)
        if room is None:
            raise HTTPException(status_code=404, detail="ROOM_NOT_FOUND")
        if room["owner_id"] != subject.user_id:
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        candidates = {account.user_id for _, account in request.app.state.auth.list_accounts()
                      if account.org_id == subject.org_id and account.active}
        if body.user_id not in candidates:
            raise HTTPException(status_code=404, detail="ACCOUNT_NOT_FOUND")
        request.app.state.meeting_rooms.add_member(subject, room_id, body.user_id)
        record_event(request, subject, "meeting.invite", "meeting_room", room_id, "success")
        return get_meeting_room(room_id, request, subject)

    @app.delete("/api/v1/meeting-rooms/{room_id}/members/{user_id}", status_code=204)
    def remove_meeting_member(room_id: str, user_id: str, request: Request,
                              subject: Subject = Depends(current_subject)):
        if not request.app.state.meeting_rooms.remove_member(subject, room_id, user_id):
            raise HTTPException(status_code=404, detail="ROOM_MEMBER_NOT_FOUND")
        record_event(request, subject, "meeting.remove_member", "meeting_room", room_id, "success")
        return Response(status_code=204)

    @app.get("/api/v1/meeting-rooms/{room_id}/messages")
    def list_meeting_messages(room_id: str, request: Request,
                              after_id: int = Query(default=0, ge=0),
                              before_id: int | None = Query(default=None, ge=1),
                              subject: Subject = Depends(current_subject)):
        messages = request.app.state.meeting_rooms.messages(subject, room_id,
            after_id=after_id, before_id=before_id)
        if messages is None:
            raise HTTPException(status_code=404, detail="ROOM_NOT_FOUND")
        return messages

    @app.post("/api/v1/meeting-rooms/{room_id}/messages", status_code=201)
    def post_meeting_message(room_id: str, body: MeetingMessageInput, request: Request,
                             subject: Subject = Depends(current_subject)):
        if not body.text.strip():
            raise HTTPException(status_code=422, detail="INVALID_MESSAGE")
        message = request.app.state.meeting_rooms.post(subject, room_id, body.text.strip())
        if message is None:
            raise HTTPException(status_code=404, detail="ROOM_NOT_FOUND")
        return message

    @app.post("/api/v1/meeting-rooms/{room_id}/minutes")
    def draft_meeting_minutes(room_id: str, request: Request,
                              subject: Subject = Depends(current_subject)):
        repository = request.app.state.meeting_rooms
        history = repository.messages(subject, room_id)
        if history is None:
            raise HTTPException(status_code=404, detail="ROOM_NOT_FOUND")
        if not history:
            raise HTTPException(status_code=409, detail="MINUTES_NO_MESSAGES")
        if request.app.state.minutes_model is None and not os.getenv("OPENAI_API_KEY", "").strip():
            raise HTTPException(status_code=503, detail="MINUTES_NOT_CONFIGURED")
        # Work from the newest bounded suffix; never send a whole room history to a model.
        selected = []
        size = 0
        for message in reversed(history[-60:]):
            if size + len(message["text"]) > 16000:
                break
            selected.append(message)
            size += len(message["text"])
        selected.reverse()
        payload = [{key: message[key] for key in ("id", "sender_id", "created_at", "text")}
                   for message in selected]
        try:
            model = request.app.state.minutes_model or OpenAIMinutesModel()
            draft = validate_minutes(model.select(payload), selected)
        except MinutesInvalid as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="MINUTES_MODEL_UNAVAILABLE") from exc
        # Membership may be revoked while the model is running.
        if repository.get(subject, room_id) is None:
            raise HTTPException(status_code=404, detail="ROOM_NOT_FOUND")
        record_event(request, subject, "meeting.minutes", "meeting_room", room_id, "success")
        return {"room_id": room_id, "source_count": len(selected),
                "first_message_id": selected[0]["id"], "last_message_id": selected[-1]["id"],
                "truncated": len(selected) < len(history) or len(history) == 100,
                **draft}

    @app.get("/api/v1/documents", response_model=list[DocumentSummary])
    def list_documents(request: Request, subject: Subject = Depends(current_subject)):
        return [document_summary(item) for item in request.app.state.documents.list_visible(subject)]

    @app.get("/api/v1/documents/{document_id}", response_model=DocumentDetail)
    def get_document(document_id: str, request: Request, subject: Subject = Depends(current_subject)):
        record = request.app.state.documents.get_visible(subject, document_id)
        if record is None:
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        return DocumentDetail(**document_summary(record).model_dump(), page=record.location, excerpt=record.excerpt)

    @app.get("/api/v1/documents/{document_id}/preview", response_model=DocumentPreview)
    def preview_document(document_id: str, request: Request, response: Response,
                         subject: Subject = Depends(current_subject)):
        source = request.app.state.documents.text_for(subject, document_id, ResourceAction.READ)
        if source is None:
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        record, text = source
        response.headers["Cache-Control"] = "private, no-store"
        return DocumentPreview(**document_summary(record).model_dump(), page=record.location,
                               excerpt=record.excerpt, preview_text=text[:6000], truncated=len(text) > 6000)

    @app.get("/api/v1/documents/{document_id}/original")
    def download_document_original(document_id: str, request: Request, version: str | None = None,
                                   subject: Subject = Depends(current_subject)):
        try:
            original = request.app.state.documents.original_for(subject, document_id, version)
        except OriginalVersionChanged as exc:
            record_event(request, subject, "document.original.download", "document", document_id, "denied")
            raise HTTPException(status_code=409, detail="DOCUMENT_VERSION_CHANGED") from exc
        if original is None:
            record_event(request, subject, "document.original.download", "document", document_id, "denied")
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        filename, file_type, content = original
        safe_name = re.split(r"[/\\]", filename)[-1]
        safe_name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "_", safe_name).strip(" .")
        extensions = {"PDF": "pdf", "DOCX": "docx", "PPTX": "pptx", "TXT": "txt", "MD": "md"}
        extension = extensions[file_type]
        if not safe_name.lower().endswith(f".{extension}"):
            safe_name = f"document-{document_id}.{extension}"
        media_types = {
            "PDF": "application/pdf",
            "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "PPTX": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "TXT": "text/plain; charset=utf-8",
            "MD": "text/markdown; charset=utf-8",
        }
        record_event(request, subject, "document.original.download", "document", document_id, "success")
        return Response(content, media_type=media_types[file_type], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @app.post("/api/v1/documents/upload", status_code=201)
    async def upload_document(request: Request, file: UploadFile = File(...), title: str = Form(...),
                              area: str = Form(...), visibility: str = Form(...),
                              subject: Subject = Depends(current_subject)):
        if subject.role not in (UserRole.DEPARTMENT_MANAGER, UserRole.KNOWLEDGE_ADMIN):
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        if not title.strip() or len(title) > 120 or area not in ("制度规范", "产品资料", "项目空间"):
            raise HTTPException(status_code=422, detail="INVALID_METADATA")
        try:
            scope = {"内部公开": Visibility.ORGANIZATION, "部门受限": Visibility.DEPARTMENT,
                     "机密": Visibility.ALLOWLIST}[visibility]
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="INVALID_METADATA") from exc
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            file_type, text = extract_text(file.filename or "", content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        document_id = request.app.state.documents.create_upload(
            subject, title=title.strip(), filename=file.filename or "", file_type=file_type,
            area=area, visibility=scope, content=content, extracted_text=text,
        )
        record_event(request, subject, "document.upload", "document", document_id, "success")
        return {"id": document_id, "status": "review"}

    @app.get("/api/v1/documents/pending/review", response_model=list[DocumentSummary])
    def pending_documents(request: Request, subject: Subject = Depends(current_subject)):
        return [document_summary(item) for item in request.app.state.documents.list_pending(subject)]

    @app.post("/api/v1/documents/{document_id}/publish", status_code=204)
    def publish_document(document_id: str, request: Request, subject: Subject = Depends(current_subject)):
        if not request.app.state.documents.publish(subject, document_id):
            record_event(request, subject, "document.publish", "document", document_id, "denied")
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        record_event(request, subject, "document.publish", "document", document_id, "success")

    @app.post("/api/v1/documents/{document_id}/versions", status_code=201)
    async def upload_document_version(document_id: str, request: Request, file: UploadFile = File(...),
                                      subject: Subject = Depends(current_subject)):
        if not request.app.state.documents.can_submit_revision(subject, document_id):
            record_event(request, subject, "document.version.upload", "document", document_id, "denied")
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            file_type, text = extract_text(file.filename or "", content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            version = request.app.state.documents.create_revision(
                subject, document_id, filename=file.filename or "", file_type=file_type,
                content=content, extracted_text=text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if version is None:
            record_event(request, subject, "document.version.upload", "document", document_id, "denied")
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        record_event(request, subject, "document.version.upload", "document", document_id, "success")
        return {"id": document_id, "version": version, "status": "review"}

    @app.get("/api/v1/documents/versions/pending")
    def pending_document_versions(request: Request, subject: Subject = Depends(current_subject)):
        return request.app.state.documents.list_pending_revisions(subject)

    @app.post("/api/v1/documents/{document_id}/versions/publish", status_code=204)
    def publish_document_version(document_id: str, request: Request, subject: Subject = Depends(current_subject)):
        version = request.app.state.documents.publish_revision(subject, document_id)
        if version is None:
            record_event(request, subject, "document.version.publish", "document", document_id, "denied")
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        record_event(request, subject, "document.version.publish", "document", document_id, "success")

    @app.post("/api/v1/answers", response_model=AnswerResponse)
    def answer(body: QuestionRequest, request: Request, subject: Subject = Depends(current_subject)):
        hits = request.app.state.documents.search_visible(subject, body.question, area=body.area, limit=6)
        if not hits:
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        paragraphs = []
        citations = []
        for hit in hits[:3]:
            sentences = relevant_sentences(body.question, hit.text, limit=2)
            if not sentences and len(search_terms(body.question, question=True) & search_terms(hit.document.title)) >= 2:
                sentences = [hit.text[:400]]
            if not sentences:
                continue
            paragraphs.append(" ".join(sentences))
            citations.append(Citation(document_id=hit.document.document_id, title=hit.document.title,
                                      version=hit.document.version, location=hit.location,
                                      excerpt=hit.text[:240]))
        if not citations:
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        return style_knowledge_answer(
            AnswerResponse(status="evidence_ready", paragraphs=paragraphs, citations=citations),
            request, subject)

    def preferred_answer_style(request: Request, subject: Subject):
        preference = next((item["value"] for item in request.app.state.memories.list_for(subject)
                           if item["kind"] == "answer_preference"), None)
        return classify_answer_style(preference)

    def style_knowledge_answer(answer: AnswerResponse, request: Request,
                               subject: Subject) -> AnswerResponse:
        if answer.status != "evidence_ready":
            return answer
        paragraphs = format_grounded_paragraphs(
            answer.paragraphs, answer.citations, preferred_answer_style(request, subject))
        return answer.model_copy(update={"paragraphs": paragraphs})

    def answer_from_work_result(question: str, result: WorkResult, request: Request,
                                subject: Subject) -> AnswerResponse:
        if result.status == "failed":
            raise HTTPException(status_code=503, detail=result.error_code or "WORKFLOW_FAILED")
        if result.status == "no_evidence":
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        paragraphs = []
        citations = []
        for evidence in result.evidence:
            record = request.app.state.documents.get_visible(subject, evidence.document_id)
            if record is None or record.version != evidence.version_id:
                continue
            excerpt = grounded_extract(question, evidence, record.title)
            if excerpt is None:
                continue
            paragraphs.append(excerpt)
            citations.append(Citation(document_id=record.document_id, title=record.title,
                                      version=record.version, location=evidence.location,
                                      excerpt=evidence.text[:240]))
            if len(citations) == 3:
                break
        if not citations:
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        return AnswerResponse(status="evidence_ready", paragraphs=paragraphs, citations=citations)

    def answer_from_react_result(result: dict, request: Request, subject: Subject) -> AnswerResponse:
        if result["status"] == "failed":
            raise HTTPException(status_code=503, detail=result.get("error_code") or "REACT_FAILED")
        citations = []
        for citation in result.get("citations", []):
            record = request.app.state.documents.get_visible(subject, citation["document_id"])
            if record is None or record.version != citation["version"]:
                raise HTTPException(status_code=409, detail="SOURCE_UNAVAILABLE")
            citations.append(Citation(document_id=record.document_id, title=record.title,
                                      version=record.version, location=citation["location"]))
        return AnswerResponse(status=result["status"], paragraphs=result["paragraphs"], citations=citations)

    def require_focus(answer: AnswerResponse, decision: IntentDecision) -> AnswerResponse:
        focus_terms = decision.focus_terms
        if not focus_terms or not answer.citations:
            return answer
        if len(answer.paragraphs) != len(answer.citations):
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        retained = [(paragraph, citation) for paragraph, citation in zip(answer.paragraphs, answer.citations)
                    if focus_supported(focus_terms, citation.title, citation.excerpt + paragraph,
                                       decision.search_query)]
        if not retained:
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        return AnswerResponse(status=answer.status, paragraphs=[item[0] for item in retained],
                              citations=[item[1] for item in retained])

    def include_document_intent(answer: AnswerResponse, decision: IntentDecision) -> AnswerResponse:
        if decision.document_requested and answer.status == "evidence_ready" and answer.citations:
            answer.document_requested = True
        return answer

    def resolve_answer_conditions(question: str, answer: AnswerResponse) -> AnswerResponse:
        if answer.status != "evidence_ready" or not answer.citations:
            return answer
        evidence = [ConditionEvidence(citation.title, (citation.excerpt or "") + "\n" +
                                      (answer.paragraphs[index] if index < len(answer.paragraphs) else ""))
                    for index, citation in enumerate(answer.citations)]
        if not relevant_condition(question, evidence):
            return answer
        decision = local_year_entitlement(question, evidence)
        if decision is None:
            try:
                decision = (condition_resolver or OpenAIConditionResolver()).resolve(question, evidence)
            except Exception as exc:
                logger.warning("Condition resolution fallback error_type=%s", type(exc).__name__)
                return answer
        validated = validate_decision(decision, evidence, question)
        if validated is None:
            return answer
        if validated.kind == "answer":
            return AnswerResponse(status="answered", paragraphs=[validated.text],
                                  citations=[answer.citations[validated.evidence_index]])
        return AnswerResponse(status="needs_clarification", paragraphs=[validated.prompt],
                              citations=[answer.citations[validated.evidence_index]], clarification={
                                  "field": validated.field, "prompt": validated.prompt,
                                  "input_type": validated.input_type, "unit": validated.unit,
                                  "options": list(validated.options), "original_question": question})

    def react_or_grounded_extract(question: QuestionRequest, request: Request,
                                  subject: Subject) -> AnswerResponse:
        """Answer exact authorized excerpts first; use ReAct for lexical misses."""
        direct = None
        try:
            grounded = run_knowledge_workflow(subject, request.app.state.documents,
                                               question.question, area=question.area)
            direct = answer_from_work_result(question.question, grounded, request, subject)
            if direct.status == "evidence_ready":
                return direct
        except Exception as exc:
            logger.warning("Direct extract raised %s; trying ReAct", type(exc).__name__)
        try:
            model = request.app.state.react_model or OpenAIReactModel()
            result = run_react(question.question, subject, request.app.state.documents,
                               model, area=question.area,
                               limits=ReactLimits(max_elapsed_seconds=30.0))
            if result["status"] != "failed":
                return answer_from_react_result(result, request, subject)
            logger.warning("ReAct failed; keeping authorized extract, reason=%s", result.get("error_code"))
        except Exception as exc:
            logger.warning("ReAct raised %s; keeping authorized extract", type(exc).__name__)
        if direct is not None:
            return direct
        raise HTTPException(status_code=503, detail="KNOWLEDGE_UNAVAILABLE")

    @app.post("/api/v1/answers/workflow", response_model=AnswerResponse)
    def workflow_answer(body: QuestionRequest, request: Request, subject: Subject = Depends(current_subject)):
        result = run_knowledge_workflow(subject, request.app.state.documents, body.question, area=body.area)
        return style_knowledge_answer(answer_from_work_result(body.question, result, request, subject),
                                      request, subject)

    @app.post("/api/v1/agent/react", response_model=ReactResponse)
    def react_answer(body: QuestionRequest, request: Request,
                     subject: Subject = Depends(current_subject)):
        if not request.app.state.react_enabled:
            raise HTTPException(status_code=503, detail="REACT_NOT_CONFIGURED")
        try:
            model = request.app.state.react_model or OpenAIReactModel()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="REACT_NOT_CONFIGURED") from exc
        result = run_react(body.question, subject, request.app.state.documents,
                           model, area=body.area)
        if result["status"] == "evidence_ready":
            result["paragraphs"] = format_grounded_paragraphs(
                result["paragraphs"], result["citations"],
                preferred_answer_style(request, subject))
        record_event(request, subject, "agent.react", "run", request.state.trace_id,
                     result["status"])
        return result

    @app.post("/api/v1/knowledge-runs", response_model=WorkflowRunResponse, status_code=201)
    def create_knowledge_run(body: QuestionRequest, request: Request,
                             subject: Subject = Depends(current_subject)):
        try:
            run = runs_repository().create(subject, request.app.state.documents, body.question, body.area)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="WORKFLOW_CHECKPOINT_FAILED") from exc
        return run

    @app.get("/api/v1/knowledge-runs/{run_id}", response_model=WorkflowRunResponse)
    def get_knowledge_run(run_id: str, request: Request, subject: Subject = Depends(current_subject)):
        run = runs_repository().get(subject, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        return run

    @app.post("/api/v1/knowledge-runs/{run_id}/resume", response_model=AnswerResponse)
    def resume_knowledge_run(run_id: str, request: Request, subject: Subject = Depends(current_subject)):
        run = runs_repository().get(subject, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        try:
            result = runs_repository().resume(subject, request.app.state.documents, run_id)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="WORKFLOW_CHECKPOINT_FAILED") from exc
        question = runs_repository().question(subject, run_id)
        return style_knowledge_answer(answer_from_work_result(question, result, request, subject),
                                      request, subject)

    @app.post("/api/v1/answers/gpt", response_model=AnswerResponse)
    def gpt_answer(body: QuestionRequest, request: Request, subject: Subject = Depends(current_subject)):
        if not request.app.state.gpt_enabled:
            raise HTTPException(status_code=503, detail="GPT_NOT_CONFIGURED")
        result = run_knowledge_workflow(subject, request.app.state.documents, body.question, area=body.area)
        if result.status == "failed":
            raise HTTPException(status_code=503, detail=result.error_code or "WORKFLOW_FAILED")
        if result.status == "no_evidence":
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        evidence = []
        for item in result.evidence:
            record = request.app.state.documents.get_visible(subject, item.document_id)
            if record is not None and record.version == item.version_id:
                evidence.append(item)
        if not evidence:
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        try:
            selector = request.app.state.quote_selector
            if selector is None:
                from app.llm.client import MODEL_NAME, create_llm_client
                selector = OpenAIQuoteSelector(create_llm_client(), MODEL_NAME)
            selected = validate_selection(selector.select(body.question, evidence), evidence)
        except ValueError as exc:
            code = "MODEL_NO_STRUCTURED_OUTPUT" if str(exc) == "MODEL_NO_STRUCTURED_OUTPUT" else "MODEL_UNSUPPORTED_QUOTE"
            raise HTTPException(status_code=502, detail=code) from exc
        except Exception as exc:
            code = "MODEL_AUTH_FAILED" if exc.__class__.__name__ == "AuthenticationError" else "MODEL_UNAVAILABLE"
            raise HTTPException(status_code=503, detail=code) from exc
        paragraphs = []
        citations = []
        for item, quote in selected:
            record = request.app.state.documents.get_visible(subject, item.document_id)
            if record is None or record.version != item.version_id:
                continue
            paragraphs.append(quote)
            citations.append(Citation(document_id=record.document_id, title=record.title,
                                      version=record.version, location=item.location,
                                      excerpt=item.text[:240]))
        if not citations:
            return AnswerResponse(status="no_evidence", paragraphs=["未找到足够的授权资料依据，无法回答。"], citations=[])
        return style_knowledge_answer(
            AnswerResponse(status="evidence_ready", paragraphs=paragraphs, citations=citations),
            request, subject)

    @app.get("/api/v1/conversations", response_model=list[ConversationSummary])
    def list_conversations(request: Request, subject: Subject = Depends(current_subject)):
        return request.app.state.conversations.list_for(subject)

    @app.get("/api/v1/conversations/{conversation_id}", response_model=ConversationDetail)
    def get_conversation(conversation_id: str, request: Request, subject: Subject = Depends(current_subject)):
        detail = request.app.state.conversations.get(subject, conversation_id, request.app.state.documents,
                                                     request.app.state.artifacts)
        if detail is None:
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        return detail

    @app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: str, request: Request,
                            subject: Subject = Depends(current_subject)):
        if not request.app.state.conversations.delete(subject, conversation_id):
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        if request.app.state.postgres_memory_enabled:
            try:
                request.app.state.memories.discard_conversation_suggestions(
                    subject, conversation_id)
            except Exception:
                # The SQLite deletion and its durable outbox event committed together.
                logger.warning("Memory source cleanup deferred to delete relay")
        else:
            request.app.state.memories.discard_conversation_suggestions(subject, conversation_id)
        record_event(request, subject, "conversation.delete", "conversation", conversation_id, "success")

    @app.get("/api/v1/conversations/workflow-runs/{run_id}")
    def conversation_workflow_run(run_id: str, request: Request, subject: Subject = Depends(current_subject)):
        if runs_repository().get(subject, run_id) is None:
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        return {"conversation_id": request.app.state.conversations.conversation_for_run(subject, run_id)}

    def suggest_after_answer(subject: Subject, question: str, conversation_id: str,
                             context: tuple[str, ...] = (),
                             job_lease: tuple[str, str] | None = None) -> None:
        repository = app.state.memories
        if (not app.state.conversations.owns(subject, conversation_id)
                or not repository.auto_suggest_enabled(subject)
                or not (eligible_auto_memory_text(question) if app.state.auto_memory_enabled
                        else eligible_user_text(question))):
            return
        try:
            suggester = app.state.memory_suggester
            if suggester is None:
                from app.llm.client import create_llm_client

                suggester = OpenAIMemorySuggester(
                    create_llm_client(timeout=25.0, max_retries=0),
                    os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini"),
                    os.getenv("GENERAL_API_STYLE", "responses"))
            analyze = getattr(suggester, "analyze", None)
            prior_auto = {memory["kind"]: memory for memory in repository.list_for(subject)
                          if memory["kind"] in ("role", "task") and memory["origin"] == "auto"
                          and memory["source_conversation_id"] != conversation_id}
            if app.state.auto_memory_enabled and callable(analyze):
                if isinstance(suggester, OpenAIMemorySuggester) and prior_auto:
                    analysis = analyze(question, context,
                                       existing_auto=tuple((kind, memory["value"])
                                                           for kind, memory in prior_auto.items()))
                else:
                    analysis = analyze(question, context)
                candidates, retractions = analysis.memories, analysis.forget
            else:
                candidates, retractions = suggester.suggest(question, context), ()
            if job_lease and not repository.job_lease_current(*job_lease):
                return
            for kind, evidence in retractions:
                if (kind in MEMORY_KINDS and isinstance(evidence, str) and evidence in question
                        and not any(candidate_kind == kind for candidate_kind, _ in candidates)
                        and app.state.conversations.owns(subject, conversation_id)):
                    if isinstance(suggester, OpenAIMemorySuggester) and kind in prior_auto:
                        if kind == "task" and repository.forget_auto_task_cross_session(
                                subject, prior_auto[kind], conversation_id, question, evidence,
                                job_lease=job_lease):
                            continue
                        if kind == "role" and repository.forget_auto_role_cross_session(
                                subject, prior_auto[kind], conversation_id, question, evidence,
                                job_lease=job_lease):
                            continue
                    repository.forget_auto_from_context(subject, kind, conversation_id, context,
                                                        job_lease=job_lease)
            for kind, value in candidates:
                # A test double must pass the same validation as model output.
                if (kind in MEMORY_KINDS and isinstance(value, str) and value in question
                        and app.state.conversations.owns(subject, conversation_id)):
                    if app.state.auto_memory_enabled:
                        repository.upsert_auto(subject, kind, value, conversation_id, question,
                                               job_lease=job_lease)
                        # The conversation and memory tables use separate SQLite files.
                        # Close the gap if deletion won between the ownership check and write.
                        if not app.state.conversations.owns(subject, conversation_id):
                            repository.discard_conversation_suggestions(subject, conversation_id)
                    else:
                        repository.propose(subject, kind, value, conversation_id, question,
                                           job_lease=job_lease)
        except Exception:
            # This runs only in the background worker; let it record and retry failures.
            raise

    def start_memory_worker() -> None:
        nonlocal memory_worker_running, memory_worker_thread
        if app.state.memory_suggester is None and not os.getenv("OPENAI_API_KEY", "").strip():
            # Keep pending jobs for the next configured API start, up to their 24-hour limit.
            return
        with memory_worker_lock:
            if memory_worker_running:
                return
            memory_worker_running = True

        def run() -> None:
            nonlocal memory_worker_running
            while not memory_worker_stop.is_set():
                with memory_worker_lock:
                    try:
                        job = app.state.memories.claim_suggestion_job()
                    except Exception:
                        if app.state.postgres_memory_enabled:
                            memory_worker_stop.wait(2)
                            continue
                        memory_worker_running = False
                        return
                    if job is None:
                        try:
                            lease_delay = app.state.memories.next_suggestion_lease_delay()
                        except Exception:
                            if app.state.postgres_memory_enabled:
                                memory_worker_stop.wait(2)
                                continue
                            memory_worker_running = False
                            return
                        if lease_delay is None:
                            if app.state.postgres_memory_enabled:
                                memory_worker_stop.wait(1)
                                continue
                            memory_worker_running = False
                            return
                if job is None:
                    memory_worker_stop.wait(min(2.0, max(0.1, lease_delay)))
                    continue
                try:
                    owner = Subject(job["owner_id"], job["org_id"], frozenset(), Classification.INTERNAL)
                    suggest_after_answer(owner, job["question"], job["source_conversation_id"],
                                         job["context"], (job["id"], job["lease_token"]))
                except Exception as error:
                    reason_code, retryable = memory_failure_code(error)
                    logger.warning("Memory extraction failed reason_code=%s", reason_code)
                    try:
                        app.state.memories.fail_suggestion_job(job["id"], job["lease_token"],
                                                               reason_code, retryable=retryable)
                    except Exception:
                        if app.state.postgres_memory_enabled:
                            memory_worker_stop.wait(2)
                            continue
                        with memory_worker_lock:
                            memory_worker_running = False
                        return
                else:
                    try:
                        app.state.memories.finish_suggestion_job(job["id"], job["lease_token"])
                    except Exception:
                        if app.state.postgres_memory_enabled:
                            memory_worker_stop.wait(2)
                            continue
                        with memory_worker_lock:
                            memory_worker_running = False
                        return
            with memory_worker_lock:
                memory_worker_running = False

        memory_worker_thread = threading.Thread(target=run, daemon=True,
                                                name="memory-suggestion")
        memory_worker_thread.start()

    def schedule_memory_suggestion(subject: Subject, question: str, conversation_id: str,
                                   detail: dict | None) -> None:
        context = tuple(turn["question"] for turn in (detail["turns"] if detail else [])
                        if turn["mode"] == "general" and eligible_auto_memory_text(turn["question"]))[-3:]
        if app.state.memories.enqueue_suggestion(subject, question, conversation_id,
                                                 include_ordinary=app.state.auto_memory_enabled,
                                                 context=context):
            start_memory_worker()

    def conversation_intent(question: str, area: str | None, previous: str | None,
                            request: Request, subject: Subject) -> IntentDecision:
        if asks_own_account(question):
            return IntentDecision("general")
        direct = high_confidence_intent(question, area=area, previous_knowledge=previous)
        if direct is not None and (direct == "general" or not request.app.state.ai_intent_enabled
                                   or not needs_deliverable_classification(question)):
            return IntentDecision(direct)
        if not request.app.state.ai_intent_enabled:
            titles = tuple(item.title for item in request.app.state.documents.list_visible(subject))
            return IntentDecision(route_intent(question, area=area, previous_knowledge=previous,
                                               visible_titles=titles))
        started = time.monotonic()
        try:
            classifier = request.app.state.intent_classifier or OpenAIIntentClassifier(
                timeout=15.0 if needs_deliverable_classification(question) else 8.0)
            classified = classifier.classify(question, previous is not None)
            if not isinstance(classified, IntentDecision) or classified.intent not in ("knowledge", "general"):
                raise ValueError("INTENT_OUTPUT_INVALID")
            logger.info("Intent model result=%s latency_ms=%d", classified.intent,
                        round((time.monotonic() - started) * 1000))
            return classified
        except Exception as exc:
            logger.warning("Intent model fallback error_type=%s latency_ms=%d",
                           type(exc).__name__, round((time.monotonic() - started) * 1000))
            titles = tuple(item.title for item in request.app.state.documents.list_visible(subject))
            return IntentDecision(route_intent(question, area=area, previous_knowledge=previous,
                                               visible_titles=titles))

    @app.post("/api/v1/conversations/answers", response_model=ConversationAnswer)
    def conversation_answer(body: ConversationQuestion, request: Request,
                            subject: Subject = Depends(current_subject)):
        if body.workflow_run_id is None and body.artifact_id is None and parse_file_access_query(body.question):
            return answer_document_permission(body, request, subject, stream=False)
        if body.mode not in ("extract", "gpt"):
            raise HTTPException(status_code=422, detail="INVALID_ANSWER_MODE")
        if body.workflow_run_id is not None and body.mode != "extract":
            raise HTTPException(status_code=422, detail="INVALID_WORKFLOW_RUN")
        if body.conversation_id is not None and not request.app.state.conversations.owns(subject, body.conversation_id):
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        detail = (request.app.state.conversations.get(subject, body.conversation_id, request.app.state.documents)
                  if body.conversation_id else None)
        last_turn = detail["turns"][-1] if detail and detail["turns"] else None
        previous = last_turn["question"] if last_turn and last_turn["mode"] in ("extract", "gpt") else None
        if body.workflow_run_id and not runs_repository().matches(
                subject, body.workflow_run_id, body.question, body.area, body.conversation_id):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        decision = (IntentDecision("knowledge") if body.workflow_run_id else
                    conversation_intent(body.question, body.area, previous, request, subject))
        intent = decision.intent
        if intent == "general":
            answer = general_answer(body.question, detail, request, subject)
            mode = "general"
        else:
            run_id = None
            question = QuestionRequest(question=knowledge_query(retrieval_query(decision, body.question), previous), area=body.area)
            if body.mode == "gpt":
                answer = gpt_answer(question, request, subject)
            elif (request.app.state.auto_react_enabled and body.workflow_run_id is None
                  and decision.search_query is None):
                answer = react_or_grounded_extract(question, request, subject)
            else:
                if body.workflow_run_id:
                    run_id = body.workflow_run_id
                    question = QuestionRequest(question=runs_repository().question(subject, run_id), area=body.area)
                else:
                    run_id = runs_repository().create(subject, request.app.state.documents,
                                                      question.question, question.area, body.conversation_id,
                                                      body.question)["id"]
                result = runs_repository().resume(subject, request.app.state.documents, run_id)
                answer = answer_from_work_result(question.question, result, request, subject)
            answer = require_focus(answer, decision)
            if body.mode != "gpt":
                answer = resolve_answer_conditions(body.question, answer)
                answer = style_knowledge_answer(answer, request, subject)
            answer = include_document_intent(answer, decision)
            mode = body.mode
        conversation_id = request.app.state.conversations.append(
            subject, body.conversation_id, body.question, mode, answer.model_dump(),
            run_id if intent == "knowledge" and body.mode == "extract" else None,
            memory_include_ordinary=(app.state.auto_memory_enabled
                                     if app.state.postgres_memory_enabled and intent == "general"
                                     and not asks_own_account(body.question)
                                     else None))
        if conversation_id is None:
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        if intent == "general" and not asks_own_account(body.question) and not app.state.postgres_memory_enabled:
            schedule_memory_suggestion(subject, body.question, conversation_id, detail)
        return ConversationAnswer(conversation_id=conversation_id, answer=answer)

    def general_chunks(question: str, detail: dict | None, request: Request,
                       subject: Subject) -> Iterator[str]:
        account_kind = account_question_kind(question)
        if account_kind is not None:
            memories = request.app.state.memories.list_for(subject) if account_kind == "identity" else []
            visible_titles = ([item.title for item in request.app.state.documents.list_visible(subject)]
                              if account_kind == "access" else None)
            yield own_account_answer(subject, question, memories, visible_titles)
            return
        fixed = local_reply(question)
        if fixed is not None:
            yield fixed
            return
        if not request.app.state.general_enabled:
            raise HTTPException(status_code=503, detail="GENERAL_CHAT_NOT_CONFIGURED")
        responder = request.app.state.general_responder
        if responder is None:
            from app.llm.client import create_llm_client
            responder = OpenAIGeneralResponder(
                create_llm_client(), os.getenv("GENERAL_OPENAI_MODEL", "gpt-4.1-mini"),
                os.getenv("GENERAL_API_STYLE", "responses"),
            )
        history = [(turn["question"][:1000], "\n".join(turn["answer"]["paragraphs"])[:1200])
                   for turn in (detail["turns"] if detail else []) if turn["mode"] == "general"][-6:]
        profile = {item["kind"]: item["value"] for item in request.app.state.memories.list_for(subject)}
        yield from responder.stream(question, history, profile)

    def general_answer(question: str, detail: dict | None, request: Request,
                       subject: Subject) -> AnswerResponse:
        if not asks_own_account(question) and local_reply(question) is None and not request.app.state.general_enabled:
            raise HTTPException(status_code=503, detail="GENERAL_CHAT_NOT_CONFIGURED")
        try:
            text = "".join(general_chunks(question, detail, request, subject)).strip()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="MODEL_UNAVAILABLE") from exc
        if not text:
            raise HTTPException(status_code=503, detail="MODEL_UNAVAILABLE")
        return AnswerResponse(status="general", paragraphs=[text], citations=[])

    @app.post("/api/v1/conversations/answers/stream")
    def stream_conversation_answer(body: ConversationQuestion, request: Request,
                                   subject: Subject = Depends(current_subject)):
        if body.workflow_run_id is None and body.artifact_id is None and parse_file_access_query(body.question):
            return answer_document_permission(body, request, subject, stream=True)
        if body.artifact_id or body.source_document_id:
            if (body.artifact_id is None) == (body.source_document_id is None) or body.workflow_run_id:
                raise HTTPException(status_code=422, detail="INVALID_FILE_TARGET")
            return stream_selected_file(body, request, subject)
        if body.mode not in ("extract", "gpt"):
            raise HTTPException(status_code=422, detail="INVALID_ANSWER_MODE")
        if body.workflow_run_id is not None and body.mode != "extract":
            raise HTTPException(status_code=422, detail="INVALID_WORKFLOW_RUN")
        detail = (request.app.state.conversations.get(subject, body.conversation_id, request.app.state.documents)
                  if body.conversation_id else None)
        if body.conversation_id and detail is None:
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        last_turn = detail["turns"][-1] if detail and detail["turns"] else None
        previous = last_turn["question"] if last_turn and last_turn["mode"] in ("extract", "gpt") else None
        if body.workflow_run_id and not runs_repository().matches(
                subject, body.workflow_run_id, body.question, body.area, body.conversation_id):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        decision = (IntentDecision("knowledge") if body.workflow_run_id else
                    conversation_intent(body.question, body.area, previous, request, subject))
        intent = decision.intent
        if (intent == "general" and not asks_own_account(body.question)
                and local_reply(body.question) is None and not request.app.state.general_enabled):
            raise HTTPException(status_code=503, detail="GENERAL_CHAT_NOT_CONFIGURED")
        answer = None
        run_id = None
        mode = "general" if intent == "general" else body.mode
        use_react = (intent == "knowledge" and body.mode == "extract"
                     and request.app.state.auto_react_enabled and body.workflow_run_id is None
                     and decision.search_query is None)
        if intent == "knowledge":
            question = QuestionRequest(question=knowledge_query(retrieval_query(decision, body.question), previous), area=body.area)
            if body.mode == "gpt":
                answer = gpt_answer(question, request, subject)
            else:
                if body.workflow_run_id:
                    run_id = body.workflow_run_id
                    question = QuestionRequest(question=runs_repository().question(subject, run_id), area=body.area)
                elif not use_react:
                    run_id = runs_repository().create(subject, request.app.state.documents,
                                                      question.question, question.area, body.conversation_id,
                                                      body.question)["id"]

        def event(kind: str, payload: dict) -> str:
            return f"data: {json.dumps({'type': kind, **payload}, ensure_ascii=False)}\n\n"

        def generate():
            yield event("meta", {"intent": intent, "workflow_run_id": run_id})
            try:
                nonlocal answer
                if use_react:
                    answer = react_or_grounded_extract(question, request, subject)
                elif run_id:
                    result = runs_repository().resume(subject, request.app.state.documents, run_id)
                    answer = answer_from_work_result(question.question, result, request, subject)
                if answer is not None:
                    answer = require_focus(answer, decision)
                    if body.mode != "gpt":
                        answer = resolve_answer_conditions(body.question, answer)
                        answer = style_knowledge_answer(answer, request, subject)
                    answer = include_document_intent(answer, decision)

                def sources_still_visible() -> bool:
                    return answer is None or all(
                        (record := request.app.state.documents.get_visible(subject, citation.document_id)) is not None
                        and record.version == citation.version for citation in answer.citations
                    )

                if answer is None:
                    chunks = general_chunks(body.question, detail, request, subject)
                else:
                    text = "\n\n".join(answer.paragraphs)
                    chunks = (text[index:index + 24] for index in range(0, len(text), 24))
                parts = []
                for chunk in chunks:
                    if not sources_still_visible():
                        raise RuntimeError("SOURCE_UNAVAILABLE")
                    parts.append(chunk)
                    yield event("delta", {"text": chunk})
                    if answer is not None:
                        time.sleep(0.025)
                final = answer or AnswerResponse(status="general", paragraphs=["".join(parts)], citations=[])
                if not sources_still_visible():
                    raise RuntimeError("SOURCE_UNAVAILABLE")
                if not "".join(parts).strip():
                    raise RuntimeError("EMPTY_ANSWER")
                conversation_id = request.app.state.conversations.append(
                    subject, body.conversation_id, body.question, mode, final.model_dump(), run_id,
                    memory_include_ordinary=(app.state.auto_memory_enabled
                                             if app.state.postgres_memory_enabled and intent == "general"
                                             and not asks_own_account(body.question)
                                             else None))
                if conversation_id is None:
                    raise RuntimeError("CONVERSATION_NOT_FOUND")
                if intent == "general" and not asks_own_account(body.question) and not app.state.postgres_memory_enabled:
                    schedule_memory_suggestion(subject, body.question, conversation_id, detail)
                yield event("done", {"conversation_id": conversation_id, "answer": final.model_dump(), "intent": intent})
            except Exception as exc:
                code = "MODEL_AUTH_FAILED" if exc.__class__.__name__ == "AuthenticationError" else "ANSWER_STREAM_FAILED"
                logger.warning("Answer stream failed trace_id=%s error_type=%s code=%s",
                               request.state.trace_id, type(exc).__name__, code)
                yield event("error", {"code": code})

        return StreamingResponse(generate(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    def answer_document_permission(body: ConversationQuestion, request: Request,
                                   subject: Subject, *, stream: bool,
                                   query: FileAccessQuery | None = None):
        if body.mode not in ("extract", "gpt"):
            raise HTTPException(status_code=422, detail="INVALID_ANSWER_MODE")
        if body.conversation_id and not request.app.state.conversations.owns(subject, body.conversation_id):
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        query = query or parse_file_access_query(body.question)
        if query is None:
            raise HTTPException(status_code=422, detail="INVALID_PERMISSION_QUESTION")
        result = answer_file_access(query, subject, request.app.state.documents, body.source_document_id)
        if result is None:
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        answer = AnswerResponse(status="file_access", paragraphs=[result.text], citations=[],
                                target_document_id=result.document_id,
                                target_document_version=result.version)

        def still_current() -> bool:
            if result.document_id is None:
                return True
            current = answer_file_access(query, subject, request.app.state.documents, result.document_id)
            return current is not None and current.version == result.version and current.text == result.text

        if not stream:
            if not still_current():
                raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED")
            conversation_id = request.app.state.conversations.append(
                subject, body.conversation_id, body.question, "file_access", answer.model_dump())
            if conversation_id is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            return ConversationAnswer(conversation_id=conversation_id, answer=answer)

        def event(kind: str, payload: dict) -> str:
            return f"data: {json.dumps({'type': kind, **payload}, ensure_ascii=False)}\n\n"

        def generate():
            yield event("meta", {"intent": "file_access", "workflow_run_id": None})
            for index in range(0, len(result.text), 24):
                if not still_current():
                    yield event("error", {"code": "SOURCE_UNAVAILABLE"})
                    return
                yield event("delta", {"text": result.text[index:index + 24]})
            if not still_current():
                yield event("error", {"code": "SOURCE_UNAVAILABLE"})
                return
            conversation_id = request.app.state.conversations.append(
                subject, body.conversation_id, body.question, "file_access", answer.model_dump())
            if conversation_id is None:
                yield event("error", {"code": "CONVERSATION_NOT_FOUND"})
                return
            yield event("done", {"conversation_id": conversation_id, "answer": answer.model_dump(),
                                 "intent": "file_access"})

        return StreamingResponse(generate(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    def stream_selected_file(body: ConversationQuestion, request: Request, subject: Subject):
        if body.conversation_id and not request.app.state.conversations.owns(subject, body.conversation_id):
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        documents = request.app.state.documents
        artifacts = request.app.state.artifacts
        if body.source_document_id:
            selected = documents.text_for(subject, body.source_document_id, ResourceAction.READ)
            if selected is None:
                raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
            record, content = selected
            version = record.version
            title = record.title
        else:
            item = artifacts.get(subject, body.artifact_id, documents)
            if item is None or item["source_status"] != "current":
                raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
            version = item["draft_token"]
            title = item["title"]
            content = "\n".join(section.get("ai_text") or section["text"]
                                for section in item["section_drafts"])

        model = None
        if request.app.state.collaboration_enabled:
            model = request.app.state.collaboration_model or OpenAICollaborationModel()
        intent = route_file_intent(body.question)
        if model is not None:
            try:
                routed = model.complete("file_intent", {"question": body.question, "file_title": title})
                if routed.get("intent") in ("answer", "edit", "permission_read", "permission_download"):
                    intent = routed["intent"]
            except Exception:
                pass  # A routing failure must never turn an ambiguous request into a write.

        if (intent in ("permission_read", "permission_download") and body.source_document_id
                and model_permission_fallback_allowed(body.question)):
            titles = re.findall(r"《([^《》]{1,120})》", body.question)
            if len(titles) <= 1:
                permission_query = FileAccessQuery(
                    ResourceAction.DOWNLOAD if intent == "permission_download" else ResourceAction.READ,
                    re.sub(r"\s+", "", titles[0]) if titles else None)
                return answer_document_permission(body, request, subject, stream=True,
                                                  query=permission_query)

        def still_current() -> bool:
            if body.source_document_id:
                current = documents.text_for(subject, body.source_document_id, ResourceAction.READ)
                return current is not None and current[0].version == version
            current = artifacts.get(subject, body.artifact_id, documents)
            return current is not None and current["source_status"] == "current" and current["draft_token"] == version

        def event(kind: str, payload: dict) -> str:
            return f"data: {json.dumps({'type': kind, **payload}, ensure_ascii=False)}\n\n"

        if intent == "edit":
            if not still_current():
                raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED")
            edited = propose_artifact_edit(ArtifactEditRequest(
                artifact_id=body.artifact_id, source_document_id=body.source_document_id,
                instruction=body.question, conversation_id=body.conversation_id), request, subject)
            answer = AnswerResponse(status="edit_proposed", paragraphs=["请核对修改内容后确认。"],
                                    citations=[], edit=edited["proposal"])

            def edit_events():
                yield event("meta", {"intent": "edit", "workflow_run_id": None})
                yield event("done", {"conversation_id": edited["conversation_id"],
                                     "answer": answer.model_dump(), "intent": "edit"})

            return StreamingResponse(edit_events(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
            })

        if not content.strip():
            paragraphs = ["所选文件没有可读取的正文。"]
            quote = ""
        else:
            evidence = relevant_sentences(body.question, content, limit=5)
            if not evidence and any(word in body.question for word in ("概括", "总结", "摘要", "主要内容", "讲什么")):
                evidence = [content[:2400]]
            quote = ""
            paragraphs = []
            if model is not None and evidence:
                try:
                    payload = {"question": body.question, "title": title,
                               "text": "\n".join(evidence)[:6000]}
                    style = preferred_answer_style(request, subject)
                    if style:
                        payload["presentation_style"] = style
                    output = model.complete("file_answer", payload)
                    candidate = output.get("quote")
                    answer_text = output.get("answer")
                    if (isinstance(candidate, str) and 0 < len(candidate) <= 450
                            and candidate in content and isinstance(answer_text, str)
                            and 0 < len(answer_text.strip()) <= 1200):
                        quote = candidate
                        paragraphs = [answer_text.strip()]
                except Exception:
                    pass
            if not paragraphs:
                paragraphs = evidence[:3] if evidence else ["所选文件中没有找到足够依据回答这个问题。"]
                quote = evidence[0][:450] if evidence else ""
        citations = ([Citation(document_id=body.source_document_id, title=title, version=version,
                               location=record.location, excerpt=quote)]
                     if body.source_document_id and quote else [])
        answer = AnswerResponse(status="answered" if quote else "no_evidence", paragraphs=paragraphs,
                                citations=citations, target_artifact_id=body.artifact_id,
                                target_artifact_token=version if body.artifact_id else None)

        def answer_events():
            yield event("meta", {"intent": "file_answer", "workflow_run_id": None})
            text = "\n\n".join(paragraphs)
            for index in range(0, len(text), 24):
                if not still_current():
                    yield event("error", {"code": "SOURCE_UNAVAILABLE"})
                    return
                yield event("delta", {"text": text[index:index + 24]})
            if not still_current():
                yield event("error", {"code": "SOURCE_UNAVAILABLE"})
                return
            conversation_id = request.app.state.conversations.append(
                subject, body.conversation_id, body.question, "file_answer", answer.model_dump(), None)
            if conversation_id is None:
                yield event("error", {"code": "CONVERSATION_NOT_FOUND"})
                return
            yield event("done", {"conversation_id": conversation_id, "answer": answer.model_dump(),
                                 "intent": "file_answer"})

        return StreamingResponse(answer_events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @app.get("/api/v1/artifacts")
    def list_artifacts(request: Request, subject: Subject = Depends(current_subject)):
        return request.app.state.artifacts.list_for(subject, request.app.state.documents)

    @app.post("/api/v1/artifacts/slide-outline")
    def suggest_slide_outline(body: SlideOutlineRequest, request: Request,
                              subject: Subject = Depends(current_subject)):
        headings = request.app.state.artifacts.suggest_slide_outline(
            subject, request.app.state.documents, body.source_ids, body.count)
        if headings is None:
            raise HTTPException(status_code=403, detail="SOURCE_NOT_ALLOWED")
        if not headings:
            raise HTTPException(status_code=422, detail="NO_EVIDENCE")
        return {"outline": headings}

    @app.post("/api/v1/artifacts", status_code=201)
    def create_artifact(body: ArtifactRequest, request: Request, subject: Subject = Depends(current_subject)):
        if (body.kind not in ("docx", "pptx") or body.theme not in ("brief", "strategy")
                or body.mode not in ("standard", "comparison")
                or (body.mode == "comparison" and (body.kind != "docx" or len(body.source_ids) < 2))
                or (body.kind == "docx" and body.theme != "brief")
                or any(not heading.strip() or len(heading) > 100 for heading in body.outline)):
            raise HTTPException(status_code=422, detail="INVALID_ARTIFACT")
        item = request.app.state.artifacts.create(
            subject, request.app.state.documents, kind=body.kind, title=body.title,
            goal=body.goal, source_ids=body.source_ids, outline=body.outline, theme=body.theme,
            mode=body.mode,
        )
        if item is None:
            record_event(request, subject, "artifact.create", "artifact", "", "denied")
            raise HTTPException(status_code=403, detail="SOURCE_NOT_ALLOWED")
        record_event(request, subject, "artifact.create", "artifact", item["id"], "success")
        return item

    @app.post("/api/v1/artifacts/compose-run", response_model=CompoundTaskResponse)
    def compose_and_run_artifact(body: ComposeFromAnswerRequest, request: Request,
                                 subject: Subject = Depends(current_subject)):
        if not request.app.state.collaboration_enabled:
            raise HTTPException(status_code=503, detail="COLLABORATION_NOT_CONFIGURED")
        if (body.conversation_id is None) != (body.turn_index is None):
            raise HTTPException(status_code=422, detail="INVALID_ANSWER_REFERENCE")
        source_hints = None
        if body.conversation_id is not None:
            detail = request.app.state.conversations.get(
                subject, body.conversation_id, request.app.state.documents)
            if detail is None or body.turn_index >= len(detail["turns"]):
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            turn = detail["turns"][body.turn_index]
            if turn["question"].strip() != body.question.strip():
                raise HTTPException(status_code=409, detail="COMPOSITION_REQUEST_CONFLICT")
            answer = turn["answer"]
            if answer["status"] != "evidence_ready" or not answer["citations"]:
                raise HTTPException(status_code=422, detail="COMPOSITION_NO_EVIDENCE")
            source_hints = [{key: citation[key] for key in ("document_id", "version", "location")}
                            for citation in answer["citations"][:6]]
        try:
            model = request.app.state.collaboration_model or OpenAICollaborationModel()
            result = compose_and_collaborate(
                subject, request.app.state.documents, request.app.state.artifacts,
                collaboration_runs_repository(), model, question=body.question,
                request_id=body.request_id,
                presentation_style=preferred_answer_style(request, subject),
                source_hints=source_hints,
                answer_reference=(f"{body.conversation_id}:{body.turn_index}"
                                  if body.conversation_id is not None else None))
        except CompositionConflict as exc:
            raise HTTPException(status_code=409, detail="COMPOSITION_REQUEST_CONFLICT") from exc
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except CompositionNoEvidence as exc:
            raise HTTPException(status_code=422, detail="COMPOSITION_NO_EVIDENCE") from exc
        except CollaborationInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="MODEL_UNAVAILABLE") from exc
        record_event(request, subject, "artifact.compose_run", "artifact",
                     result["artifact"]["id"], "success")
        return result

    @app.post("/api/v1/artifacts/compose", status_code=201)
    def compose_artifact(body: ComposeFromQuestionRequest, request: Request,
                         subject: Subject = Depends(current_subject)):
        try:
            item = request.app.state.artifacts.create_from_question(
                subject, request.app.state.documents, question=body.question,
                request_id=body.request_id)
        except CompositionConflict as exc:
            record_event(request, subject, "artifact.compose", "artifact", "", "denied")
            raise HTTPException(status_code=409, detail="COMPOSITION_REQUEST_CONFLICT") from exc
        except SourceVersionChanged as exc:
            record_event(request, subject, "artifact.compose", "artifact", "", "denied")
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except CompositionNoEvidence as exc:
            record_event(request, subject, "artifact.compose", "artifact", "", "denied")
            raise HTTPException(status_code=422, detail="COMPOSITION_NO_EVIDENCE") from exc
        record_event(request, subject, "artifact.compose", "artifact", item["id"], "success")
        return item

    @app.get("/api/v1/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str, request: Request, subject: Subject = Depends(current_subject)):
        item = request.app.state.artifacts.get(subject, artifact_id, request.app.state.documents)
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        return item

    @app.post("/api/v1/conversations/artifact-edits")
    def propose_artifact_edit(body: ArtifactEditRequest, request: Request,
                              subject: Subject = Depends(current_subject)):
        if (body.artifact_id is None) == (body.source_document_id is None):
            raise HTTPException(status_code=422, detail="INVALID_EDIT_TARGET")
        if (body.conversation_id is not None and
                not request.app.state.conversations.owns(subject, body.conversation_id)):
            raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
        if not request.app.state.collaboration_enabled:
            raise HTTPException(status_code=503, detail="COLLABORATION_NOT_CONFIGURED")
        copy_id = None
        proposal = None
        try:
            artifact_id = body.artifact_id
            if body.source_document_id is not None:
                copy = request.app.state.artifacts.create_editable_copy(
                    subject, body.source_document_id, request.app.state.documents)
                if copy is None:
                    raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
                artifact_id = copy["id"]
                copy_id = artifact_id
            model = request.app.state.collaboration_model or OpenAICollaborationModel()
            proposal = request.app.state.artifacts.propose_edit(
                subject, artifact_id, request.app.state.documents, body.instruction, model)
        except HTTPException:
            raise
        except EditSourceTooLarge as exc:
            raise HTTPException(status_code=422, detail="EDIT_SOURCE_TOO_LARGE") from exc
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except InvalidEdit as exc:
            raise HTTPException(status_code=422, detail="INVALID_EDIT") from exc
        except CollaborationInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="MODEL_UNAVAILABLE") from exc
        finally:
            if copy_id is not None and proposal is None:
                request.app.state.artifacts.discard_editable_copy(subject, copy_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        conversation_id = request.app.state.conversations.append(
            subject, body.conversation_id, body.instruction, "artifact_edit",
            {"proposal_id": proposal["id"], "status": "edit_proposed", "paragraphs": [], "citations": []})
        record_event(request, subject, "artifact.edit.propose", "artifact", proposal["artifact_id"], "success")
        return {"conversation_id": conversation_id, "proposal": proposal}

    @app.post("/api/v1/artifact-edit-proposals/{proposal_id}/confirm")
    def confirm_artifact_edit(proposal_id: str, request: Request,
                              subject: Subject = Depends(current_subject)):
        try:
            item = request.app.state.artifacts.apply_edit_proposal(subject, proposal_id, request.app.state.documents)
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        if item is None:
            raise HTTPException(status_code=404, detail="EDIT_PROPOSAL_NOT_FOUND")
        record_event(request, subject, "artifact.edit.confirm", "artifact", item["id"], "success")
        return item

    @app.post("/api/v1/artifacts/{artifact_id}/preview")
    def preview_artifact(artifact_id: str, body: ConfirmRequest, request: Request,
                         subject: Subject = Depends(current_subject)):
        if any(not heading.strip() or len(heading) > 100 for heading in body.outline):
            raise HTTPException(status_code=422, detail="INVALID_ARTIFACT")
        try:
            item = request.app.state.artifacts.preview(subject, artifact_id, request.app.state.documents, body.outline)
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        return item

    @app.post("/api/v1/artifacts/{artifact_id}/review")
    def review_artifact(artifact_id: str, body: ReviewRequest, request: Request,
                        subject: Subject = Depends(current_subject)):
        try:
            item = request.app.state.artifacts.review(
                subject, artifact_id, request.app.state.documents,
                [review.model_dump() for review in body.reviews],
            )
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except InvalidReview as exc:
            raise HTTPException(status_code=422, detail="INVALID_REVIEW") from exc
        if item is None:
            record_event(request, subject, "artifact.review", "artifact", artifact_id, "denied")
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.review", "artifact", artifact_id, "success")
        return item

    @app.post("/api/v1/artifacts/{artifact_id}/notes")
    def save_artifact_notes(artifact_id: str, body: NotesRequest, request: Request,
                            subject: Subject = Depends(current_subject)):
        try:
            item = request.app.state.artifacts.save_notes(
                subject, artifact_id, request.app.state.documents,
                [note.model_dump() for note in body.notes], body.draft_token,
            )
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except InvalidNote as exc:
            raise HTTPException(status_code=422, detail="INVALID_NOTE") from exc
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.notes", "artifact", artifact_id, "success")
        return item

    @app.post("/api/v1/artifacts/{artifact_id}/collaborate")
    def collaborate_artifact(artifact_id: str, body: CollaborationRequest, request: Request,
                             subject: Subject = Depends(current_subject)):
        item = request.app.state.artifacts.get(subject, artifact_id, request.app.state.documents)
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        if not request.app.state.collaboration_enabled:
            raise HTTPException(status_code=503, detail="COLLABORATION_NOT_CONFIGURED")
        try:
            model = request.app.state.collaboration_model or OpenAICollaborationModel()
            result = collaboration_runs_repository().execute(
                subject, artifact_id, request.app.state.artifacts, request.app.state.documents, model,
                body.draft_token, body.request_id, preferred_answer_style(request, subject))
            if result is not None and request.app.state.auto_reflection_enabled:
                reflection_model = request.app.state.reflection_model or OpenAIReflectionModel()
                try:
                    reflected = request.app.state.artifacts.reflect(
                        subject, artifact_id, request.app.state.documents, reflection_model,
                        result["draft_token"], f"{body.request_id}:reflection",
                        presentation_style=preferred_answer_style(request, subject))
                    if reflected is not None:
                        result = reflected
                except ReflectionInvalid as exc:
                    # Keep the grounded AI draft available; the UI can retry reflection explicitly.
                    result = dict(result)
                    result["reflection_error"] = str(exc)
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except CollaborationInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="MODEL_UNAVAILABLE") from exc
        if result is None:
            record_event(request, subject, "artifact.collaborate", "artifact", artifact_id, "denied")
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.collaborate", "artifact", artifact_id, "success")
        return result

    @app.post("/api/v1/artifacts/{artifact_id}/slide-visuals")
    def generate_slide_visuals(artifact_id: str, body: SlideVisualRequest, request: Request,
                               subject: Subject = Depends(current_subject)):
        try:
            item = request.app.state.artifacts.generate_slide_visuals(
                subject, artifact_id, request.app.state.documents, body.draft_token,
                request.app.state.slide_photo_provider)
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.slide_visuals", "artifact", artifact_id, "success")
        return item

    @app.get("/api/v1/artifacts/{artifact_id}/slide-visuals/{page_index}/image")
    def get_slide_visual_image(artifact_id: str, page_index: int, request: Request,
                               subject: Subject = Depends(current_subject)):
        image = request.app.state.artifacts.slide_visual_image(
            subject, artifact_id, page_index, request.app.state.documents)
        if image is None:
            raise HTTPException(status_code=404, detail="SLIDE_IMAGE_NOT_FOUND")
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.post("/api/v1/artifacts/{artifact_id}/reflect")
    def reflect_artifact(artifact_id: str, body: ReflectionRequest, request: Request,
                         subject: Subject = Depends(current_subject)):
        item = request.app.state.artifacts.get(subject, artifact_id, request.app.state.documents)
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        if not request.app.state.reflection_enabled:
            raise HTTPException(status_code=503, detail="REFLECTION_NOT_CONFIGURED")
        try:
            model = request.app.state.reflection_model or OpenAIReflectionModel()
            result = request.app.state.artifacts.reflect(
                subject, artifact_id, request.app.state.documents, model,
                body.draft_token, body.request_id,
                presentation_style=preferred_answer_style(request, subject))
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except ReflectionInvalid as exc:
            code = str(exc)
            status = 503 if code in ("MODEL_UNAVAILABLE", "REFLECTION_BUDGET_EXCEEDED") else 422
            raise HTTPException(status_code=status, detail=code) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="MODEL_UNAVAILABLE") from exc
        if result is None:
            record_event(request, subject, "artifact.reflect", "artifact", artifact_id, "denied")
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.reflect", "artifact", artifact_id, "success")
        return result

    @app.get("/api/v1/artifacts/{artifact_id}/collaboration-run", response_model=CollaborationRunResponse)
    def get_collaboration_run(artifact_id: str, request: Request,
                              subject: Subject = Depends(current_subject)):
        item = request.app.state.artifacts.get(subject, artifact_id, request.app.state.documents)
        if item is None:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        run = collaboration_runs_repository().get_latest(
            subject, artifact_id, request.app.state.artifacts, request.app.state.documents)
        if run is None:
            raise HTTPException(status_code=404, detail="COLLABORATION_RUN_NOT_FOUND")
        return run

    @app.post("/api/v1/artifacts/{artifact_id}/confirm")
    def confirm_artifact(artifact_id: str, body: ConfirmRequest, request: Request,
                         subject: Subject = Depends(current_subject)):
        if any(not heading.strip() or len(heading) > 100 for heading in body.outline):
            raise HTTPException(status_code=422, detail="INVALID_ARTIFACT")
        try:
            item = request.app.state.artifacts.confirm(subject, artifact_id, request.app.state.documents,
                                                       body.outline, body.draft_token)
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        except DraftChanged as exc:
            raise HTTPException(status_code=409, detail="DRAFT_CHANGED") from exc
        except ReviewRequired as exc:
            raise HTTPException(status_code=409, detail="REVIEW_REQUIRED") from exc
        if item is None:
            record_event(request, subject, "artifact.confirm", "artifact", artifact_id, "denied")
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.confirm", "artifact", artifact_id, "success")
        return item

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str, request: Request, subject: Subject = Depends(current_subject)):
        try:
            result = request.app.state.artifacts.download(subject, artifact_id, request.app.state.documents)
        except SourceVersionChanged as exc:
            record_event(request, subject, "artifact.download", "artifact", artifact_id, "denied")
            raise HTTPException(status_code=409, detail="SOURCE_VERSION_CHANGED") from exc
        if result is None:
            record_event(request, subject, "artifact.download", "artifact", artifact_id, "denied")
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        record_event(request, subject, "artifact.download", "artifact", artifact_id, "success")
        kind, content = result
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if kind == "docx" else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        return Response(content, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{artifact_id}.{kind}"'})

    @app.put("/api/v1/admin/documents/{document_id}/deny", status_code=204)
    def set_document_deny(document_id: str, body: AccessChange, request: Request, subject: Subject = Depends(current_subject)):
        if subject.role != UserRole.SYSTEM_ADMIN:
            raise HTTPException(status_code=403, detail="FORBIDDEN")
        valid_user = any(
            account.user_id == body.user_id and account.org_id == subject.org_id
            for _, account in request.app.state.auth.list_accounts()
        )
        if not valid_user:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
        if not request.app.state.documents.set_denied(subject.org_id, document_id, body.user_id, body.denied):
            raise HTTPException(status_code=404, detail="DOCUMENT_NOT_FOUND")
        record_event(request, subject, "document.deny" if body.denied else "document.allow",
                     "document", document_id, "success")

    return app


app = create_app()
