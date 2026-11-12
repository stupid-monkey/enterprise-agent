import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.sqlite import SqliteSaver

from app.api.documents import DocumentRepository
from app.api.workflow_answers import KnowledgePlanner, SubjectSearch
from app.security.policy import Classification, Subject, UserRole
from app.workflow.graph import Evidence, Plan, build_workflow


class FakePlanner:
    def __init__(self, plan):
        self.value = plan

    def plan(self, question):
        return self.value


class FakeSearch:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.results


class WorkflowTests(unittest.TestCase):
    def test_knowledge_route_returns_evidence(self):
        evidence = Evidence(document_id="policy", version_id="v1", location="p2", text="Travel approval")
        search = FakeSearch([evidence])
        graph = build_workflow(FakePlanner(Plan(action="knowledge", query="travel")), search)

        state = graph.invoke({"question": "Travel policy?", "steps": 0})

        self.assertEqual(state["result"]["status"], "evidence_ready")
        self.assertEqual(state["result"]["evidence"][0]["document_id"], "policy")
        self.assertEqual(search.queries, ["travel"])
        self.assertEqual(state["steps"], 3)

    def test_document_route_only_collects_sources(self):
        search = FakeSearch([Evidence(document_id="report", version_id="v2", location="s1", text="Progress")])
        graph = build_workflow(FakePlanner(Plan(action="document", query="weekly report")), search)

        state = graph.invoke({"question": "Create a weekly report", "steps": 0})

        self.assertEqual(state["result"]["status"], "evidence_ready")
        self.assertNotIn("artifact_id", state["result"])

    def test_invalid_plan_fails_closed_without_search(self):
        search = FakeSearch([])
        graph = build_workflow(FakePlanner({"action": "delete_all", "query": "x"}), search)

        state = graph.invoke({"question": "Delete files", "steps": 0})

        self.assertEqual(state["result"]["error_code"], "INVALID_PLAN")
        self.assertEqual(search.queries, [])

    def test_no_evidence_refuses_to_answer(self):
        graph = build_workflow(FakePlanner(Plan(action="knowledge", query="unknown")), FakeSearch([]))

        state = graph.invoke({"question": "Unknown policy", "steps": 0})

        self.assertEqual(state["result"]["status"], "no_evidence")

    def test_invalid_search_result_does_not_escape(self):
        graph = build_workflow(FakePlanner(Plan(action="knowledge", query="policy")), FakeSearch([{"text": "unattributed"}]))

        state = graph.invoke({"question": "Policy?", "steps": 0})

        self.assertEqual(state["result"]["error_code"], "SEARCH_FAILED")
        self.assertEqual(state["result"]["evidence"], [])

    def test_search_exception_returns_structured_failure(self):
        class BrokenSearch:
            def search(self, query):
                raise TimeoutError("temporary service outage")

        graph = build_workflow(FakePlanner(Plan(action="knowledge", query="policy")), BrokenSearch())

        state = graph.invoke({"question": "Policy?", "steps": 0})

        self.assertEqual(state["result"]["error_code"], "SEARCH_FAILED")

    def test_step_budget_is_enforced(self):
        graph = build_workflow(FakePlanner(Plan(action="knowledge", query="policy")), FakeSearch([]), max_steps=3)

        with self.assertRaisesRegex(RuntimeError, "step budget"):
            graph.invoke({"question": "Policy?", "steps": 1})

    def test_sqlite_checkpoint_resumes_after_reopen_and_rechecks_revocation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.sqlite3"
            documents = DocumentRepository(":memory:", seed_demo=True)
            manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                              Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
            config = {"configurable": {"thread_id": "isolated-test-run"}}

            def graph_with(saver):
                return build_workflow(
                    KnowledgePlanner(), SubjectSearch(documents, manager),
                    checkpointer=saver, interrupt_before=["validate"],
                    validate_evidence=lambda item: (
                        (record := documents.get_visible(manager, item.document_id)) is not None
                        and record.version == item.version_id
                    ),
                )

            try:
                with SqliteSaver.from_conn_string(str(path)) as saver:
                    graph = graph_with(saver)
                    state = graph.invoke({"question": "产品发布前有哪些检查项？", "steps": 0}, config)
                    self.assertEqual(state["evidence"][0]["document_id"], "5")
                    self.assertEqual(graph.get_state(config).next, ("validate",))

                documents.set_denied("demo-org", "5", manager.user_id, True)
                with SqliteSaver.from_conn_string(str(path)) as saver:
                    graph = graph_with(saver)
                    resumed = graph.invoke(None, config)
                    self.assertEqual(resumed["result"]["status"], "no_evidence")
                    self.assertEqual(resumed["result"]["evidence"], [])
                    self.assertEqual(graph.get_state(config).next, ())
            finally:
                documents.close()


if __name__ == "__main__":
    unittest.main()
