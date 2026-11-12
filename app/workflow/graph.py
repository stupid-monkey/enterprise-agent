"""Offline-testable graph boundary for authorized knowledge and document work."""

from typing import Callable, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["knowledge", "document"]
    query: str = Field(min_length=1, max_length=2000)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    text: str = Field(min_length=1)


class WorkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["evidence_ready", "no_evidence", "failed"]
    text: str
    evidence: list[Evidence] = Field(default_factory=list)
    error_code: str | None = None


class Planner(Protocol):
    def plan(self, question: str) -> Plan: ...


class AuthorizedSearch(Protocol):
    def search(self, query: str) -> list[Evidence]: ...


class WorkflowState(TypedDict, total=False):
    question: str
    plan: dict
    evidence: list[dict]
    result: dict
    error_code: str
    steps: int


def build_workflow(
    planner: Planner,
    search: AuthorizedSearch,
    *,
    max_steps: int = 4,
    checkpointer=None,
    interrupt_before: list[str] | None = None,
    validate_evidence: Callable[[Evidence], bool] | None = None,
):
    """Build a read-only graph; search must already enforce caller authorization.

    This graph has no identity source or document generator. Do not expose it as
    a multi-user API until a trusted service injects an authorized search adapter.
    """
    if max_steps < 3:
        raise ValueError("max_steps must allow planning, retrieval, and validation")

    def advance(state: WorkflowState) -> int:
        count = state.get("steps", 0) + 1
        if count > max_steps:
            raise RuntimeError("workflow step budget exhausted")
        return count

    def supervisor(state: WorkflowState) -> WorkflowState:
        count = advance(state)
        try:
            plan = Plan.model_validate(planner.plan(state["question"]))
            return {"plan": plan.model_dump(), "steps": count}
        except Exception:
            return {"error_code": "INVALID_PLAN", "steps": count}

    def route(state: WorkflowState) -> str:
        if state.get("error_code"):
            return "validate"
        return state["plan"]["action"]

    def retrieve(state: WorkflowState) -> WorkflowState:
        count = advance(state)
        try:
            items = [Evidence.model_validate(item) for item in search.search(state["plan"]["query"])]
            return {"evidence": [item.model_dump() for item in items], "steps": count}
        except Exception:
            return {"error_code": "SEARCH_FAILED", "steps": count}

    def knowledge(state: WorkflowState) -> WorkflowState:
        return retrieve(state)

    def document(state: WorkflowState) -> WorkflowState:
        return retrieve(state)

    def validate(state: WorkflowState) -> WorkflowState:
        count = advance(state)
        if state.get("error_code"):
            result = WorkResult(status="failed", text="任务未能完成。", error_code=state["error_code"])
        else:
            evidence = [Evidence.model_validate(item) for item in state.get("evidence", [])]
            if validate_evidence is not None:
                evidence = [item for item in evidence if validate_evidence(item)]
            if evidence:
                result = WorkResult(
                    status="evidence_ready",
                    text="已收集授权资料；回答和文档草稿尚未生成。",
                    evidence=evidence,
                )
            else:
                result = WorkResult(status="no_evidence", text="未找到可靠的授权资料，无法回答或生成草稿。")
        return {"result": result.model_dump(), "steps": count}

    graph = StateGraph(WorkflowState)
    graph.add_node("supervisor", supervisor)
    graph.add_node("knowledge", knowledge)
    graph.add_node("document", document)
    graph.add_node("validate", validate)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route, {"knowledge": "knowledge", "document": "document", "validate": "validate"})
    graph.add_edge("knowledge", "validate")
    graph.add_edge("document", "validate")
    graph.add_edge("validate", END)
    return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)
