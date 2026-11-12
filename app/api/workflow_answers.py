"""Request-scoped bridge from authenticated API subjects to the read-only graph."""

from dataclasses import dataclass

from app.api.content import relevant_sentences, search_terms
from app.api.documents import DocumentRepository
from app.security.policy import Subject
from app.workflow.graph import Evidence, Plan, Planner, WorkResult, build_workflow


class KnowledgePlanner:
    def plan(self, question: str) -> Plan:
        return Plan(action="knowledge", query=question)


@dataclass
class SubjectSearch:
    documents: DocumentRepository
    subject: Subject
    area: str | None = None

    def search(self, query: str) -> list[Evidence]:
        return [
            Evidence(document_id=hit.document.document_id, version_id=hit.document.version,
                     location=hit.location, text=hit.text)
            for hit in self.documents.search_visible(self.subject, query, area=self.area, limit=6)
        ]


def run_knowledge_workflow(subject: Subject, documents: DocumentRepository, question: str,
                           *, area: str | None = None, planner: Planner | None = None) -> WorkResult:
    graph = build_workflow(
        planner or KnowledgePlanner(), SubjectSearch(documents, subject, area),
        validate_evidence=lambda item: (
            (record := documents.get_visible(subject, item.document_id)) is not None
            and record.version == item.version_id
        ),
    )
    state = graph.invoke({"question": question, "steps": 0})
    return WorkResult.model_validate(state["result"])


def grounded_extract(question: str, evidence: Evidence, title: str) -> str | None:
    sentences = relevant_sentences(question, evidence.text, limit=2)
    if not sentences and len(search_terms(question, question=True) & search_terms(title)) >= 2:
        sentences = [evidence.text[:400]]
    return " ".join(sentences) if sentences else None
