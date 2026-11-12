"""Question-to-document handoff uses current authorization and idempotent task creation."""

import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.collaboration_runs import CollaborationRunRepository
from app.api.compound_tasks import compose_and_collaborate
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class FakeCollaborationModel:
    def __init__(self):
        self.calls = []

    def complete(self, stage, payload):
        self.calls.append(stage)
        if stage == "compose_supervisor":
            return {"title": payload["question"][:80] + " · 资料整理",
                    "source_ids": [source["document_id"] for source in payload["candidates"][:2]],
                    "headings": [payload["question"][:100]],
                    "focus": [payload["question"][:100]]}
        if stage == "supervisor":
            return {"focus": payload["headings"]}
        if stage == "knowledge":
            if "evidence_sections" in payload:
                return {"source_quotes": [[source["text"][:20] for source in sources]
                                          for sources in payload["evidence_sections"]]}
            return {"quotes": [text[:20] for text in payload["excerpts"]]}
        return {"drafts": ["根据原文整理：" + quote if quote else "" for quote in payload["quotes"]]}


class FailOnceCollaborationModel(FakeCollaborationModel):
    def __init__(self):
        super().__init__()
        self.failed = False

    def complete(self, stage, payload):
        if stage == "knowledge" and not self.failed:
            self.calls.append(stage)
            self.failed = True
            raise RuntimeError("synthetic model outage")
        return super().complete(stage, payload)


class SlowPlannerModel(FakeCollaborationModel):
    def complete(self, stage, payload):
        if stage == "compose_supervisor":
            time.sleep(0.1)
        return super().complete(stage, payload)


class ArtifactCompositionTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.artifacts = ArtifactRepository(":memory:")
        self.model = FakeCollaborationModel()
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("colleague", hash_password("password"), Subject(
                "employee-002", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("manager", hash_password("password"), Subject(
                "manager-001", "demo-org", frozenset({"product"}), Classification.DEPARTMENT,
                role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("admin", hash_password("password"), Subject(
                "admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED,
                role=UserRole.SYSTEM_ADMIN)),
        ]
        self.client = TestClient(create_app(LocalAuth(accounts), self.documents,
                                            artifacts=self.artifacts, collaboration_model=self.model))
        self.employee = self._login("employee")
        self.colleague = self._login("colleague")
        self.manager = self._login("manager")
        self.admin = self._login("admin")

    def _login(self, name):
        token = self.client.post("/api/v1/auth/login", json={
            "username": name, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _compose(self, question="差旅报销审批流程", request_id="answer-doc-001", headers=None):
        return self.client.post("/api/v1/artifacts/compose", headers=headers or self.employee,
                                json={"question": question, "request_id": request_id})

    def _compose_run(self, question="差旅报销审批流程", request_id="answer-doc-run-001", headers=None):
        return self.client.post("/api/v1/artifacts/compose-run", headers=headers or self.employee,
                                json={"question": question, "request_id": request_id})

    def test_compound_request_completes_and_replays_without_model_calls(self):
        response = self._compose_run()
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["artifact"]["source_ids"], ["1"])
        self.assertEqual([step["agent"] for step in result["run"]["trace"]],
                         ["supervisor", "knowledge", "document", "validator"])
        self.assertEqual(self.model.calls, ["compose_supervisor", "knowledge", "document"])
        self.assertTrue(result["artifact"]["section_drafts"][0]["ai_text"])
        replay = self._compose_run().json()
        self.assertEqual(replay["artifact"]["id"], result["artifact"]["id"])
        self.assertFalse(replay["artifact"]["composition_created"])
        self.assertEqual(self.model.calls, ["compose_supervisor", "knowledge", "document"])
        self.assertEqual(self._compose_run("员工年假标准").status_code, 409)

    def test_compound_document_receives_bounded_cross_session_style(self):
        self.client.put("/api/v1/me/memories/answer_preference", headers=self.employee,
                        json={"value": "请分点回答"})
        original = self.model.complete
        document_payloads = []

        def capture(stage, payload):
            if stage == "document":
                document_payloads.append(payload.copy())
            return original(stage, payload)

        self.model.complete = capture
        response = self._compose_run(request_id="style-compound")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(document_payloads[0]["presentation_style"], "bullets")
        self.assertNotIn("请分点回答", str(document_payloads))

    def test_compound_failure_is_visible_and_explicit_retry_resumes(self):
        self.model = FailOnceCollaborationModel()
        self.client.app.state.collaboration_model = self.model
        first = self._compose_run().json()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["run"]["stage"], "knowledge")
        self.assertEqual(first["run"]["attempts"], 1)
        self.assertEqual([step["agent"] for step in first["run"]["trace"]], ["supervisor"])
        self.assertEqual(first["artifact"]["status"], "pending")
        self.assertEqual(len(self.client.get("/api/v1/artifacts", headers=self.employee).json()), 1)
        replay = self._compose_run().json()
        self.assertEqual(replay["status"], "failed")
        self.assertEqual(self.model.calls, ["compose_supervisor", "knowledge"])

        item = first["artifact"]
        resumed = self.client.post(f"/api/v1/artifacts/{item['id']}/collaborate",
                                   headers=self.employee, json={"draft_token": item["draft_token"],
                                                                "request_id": "answer-doc-run-001"})
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(self.model.calls,
                         ["compose_supervisor", "knowledge", "knowledge", "document"])
        self.assertEqual(len(resumed.json()["collaboration_trace"]), 4)

    def test_compound_rechecks_authorization_on_replay(self):
        self.assertEqual(self.client.post("/api/v1/artifacts/compose-run", json={
            "question": "差旅报销审批流程", "request_id": "answer-doc-run-001"}).status_code, 401)
        self.assertEqual(self._compose_run("完全无关的银河主题").json()["code"],
                         "COMPOSITION_NO_EVIDENCE")
        result = self._compose_run().json()
        artifact_id = result["artifact"]["id"]
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}",
                                         headers=self.colleague).status_code, 404)
        calls = list(self.model.calls)
        self.documents.set_denied("demo-org", "1", "employee-001", True)
        self.assertEqual(self._compose_run().json()["code"], "COMPOSITION_NO_EVIDENCE")
        self.assertEqual(self.model.calls, calls)

    def test_planner_selects_multiple_authorized_sources(self):
        result = self._compose_run("知识检索权限治理和文档生成", headers=self.manager).json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["artifact"]["source_ids"]), {"3", "4"})
        self.assertEqual(set(result["run"]["trace"][0]["output"]["source_ids"]), {"3", "4"})
        self.assertEqual(len(result["artifact"]["section_drafts"][0]["ai_evidence"]), 2)
        self.assertEqual(self._compose_run("知识检索权限治理和文档生成").json()["code"],
                         "COMPOSITION_NO_EVIDENCE")

    def test_invalid_or_stale_plan_never_creates_artifact(self):
        original = self.model.complete

        def invalid_source(stage, payload):
            if stage == "compose_supervisor":
                return {"title": "无权来源", "source_ids": ["999"],
                        "headings": [payload["question"]], "focus": [payload["question"]]}
            return original(stage, payload)

        self.model.complete = invalid_source
        invalid = self._compose_run()
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "COMPOSITION_PLAN_INVALID")
        self.assertEqual(self.client.get("/api/v1/artifacts", headers=self.employee).json(), [])

        def changed_version(stage, payload):
            if stage == "compose_supervisor":
                result = original(stage, payload)
                self.documents._db.execute("UPDATE documents SET version = 'v99' WHERE document_id = '1'")
                self.documents._db.commit()
                return result
            return original(stage, payload)

        self.model.complete = changed_version
        stale = self._compose_run(request_id="answer-doc-run-002")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "SOURCE_VERSION_CHANGED")
        self.assertEqual(self.client.get("/api/v1/artifacts", headers=self.employee).json(), [])

    def test_stale_search_hit_is_not_sent_to_planner(self):
        subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        old_hits = self.documents.search_visible(subject, "差旅报销审批流程", limit=12)
        self.documents._db.execute("UPDATE documents SET version = 'v99' WHERE document_id = '1'")
        self.documents._db.commit()
        self.documents.search_visible = lambda *_args, **_kwargs: old_hits
        response = self._compose_run()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "SOURCE_VERSION_CHANGED")
        self.assertEqual(self.model.calls, [])

    def test_editing_planned_outline_invalidates_old_plan(self):
        item = self._compose_run().json()["artifact"]
        preview = self.client.post(f"/api/v1/artifacts/{item['id']}/preview",
                                   headers=self.employee, json={"outline": ["差旅报销审批流程"]})
        self.assertEqual(preview.status_code, 200)
        self.assertIsNone(preview.json()["composition_plan"])
        regenerated = self.client.post(f"/api/v1/artifacts/{item['id']}/collaborate",
                                       headers=self.employee, json={"draft_token": preview.json()["draft_token"],
                                                                    "request_id": "new-outline-run-001"})
        self.assertEqual(regenerated.status_code, 200)
        self.assertEqual(self.model.calls,
                         ["compose_supervisor", "knowledge", "document",
                          "supervisor", "knowledge", "document"])

    def test_concurrent_replay_runs_planner_once_in_one_process(self):
        model = SlowPlannerModel()
        runs = CollaborationRunRepository(":memory:")
        subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        try:
            def invoke():
                return compose_and_collaborate(
                    subject, self.documents, self.artifacts, runs, model,
                    question="差旅报销审批流程", request_id="concurrent-run-001")

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: invoke(), range(2)))
            self.assertEqual(results[0]["artifact"]["id"], results[1]["artifact"]["id"])
            self.assertEqual(model.calls, ["compose_supervisor", "knowledge", "document"])
        finally:
            runs.close()

    def test_question_creates_reviewable_document_and_reuses_collaboration(self):
        response = self._compose()
        self.assertEqual(response.status_code, 201)
        item = response.json()
        self.assertEqual(item["source_ids"], ["1"])
        self.assertTrue(item["composition_created"])
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["source_status"], "current")
        self.assertEqual(item["outline"], ["差旅报销审批流程"])
        replay = self._compose().json()
        self.assertEqual(replay["id"], item["id"])
        self.assertFalse(replay["composition_created"])
        self.assertEqual(len(self.client.get("/api/v1/artifacts", headers=self.employee).json()), 1)

        path = f"/api/v1/artifacts/{item['id']}/collaborate"
        body = {"draft_token": item["draft_token"], "request_id": "answer-doc-001"}
        collaborated = self.client.post(path, headers=self.employee, json=body)
        self.assertEqual(collaborated.status_code, 200)
        self.assertEqual(self.model.calls, ["supervisor", "knowledge", "document"])
        self.assertTrue(collaborated.json()["section_drafts"][0]["ai_text"])
        self.assertEqual(self.client.post(path, headers=self.employee, json=body).status_code, 200)
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(self._compose().json()["id"], item["id"])

    def test_same_request_cannot_change_question_and_owner_is_scoped(self):
        item = self._compose().json()
        conflict = self._compose("员工年假标准", request_id="answer-doc-001")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "COMPOSITION_REQUEST_CONFLICT")
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{item['id']}",
                                         headers=self.colleague).status_code, 404)
        colleague_item = self._compose(headers=self.colleague).json()
        self.assertNotEqual(colleague_item["id"], item["id"])

    def test_no_authorized_evidence_does_not_create_task(self):
        self.assertEqual(self.client.post("/api/v1/artifacts/compose", json={
            "question": "差旅报销审批流程", "request_id": "answer-doc-001"}).status_code, 401)
        response = self._compose("完全无关的银河主题")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "COMPOSITION_NO_EVIDENCE")
        self.assertEqual(self.client.get("/api/v1/artifacts", headers=self.employee).json(), [])
        self.assertEqual(self.model.calls, [])

    def test_department_sources_are_selected_only_for_authorized_employee(self):
        question = "知识检索权限治理和文档生成"
        self.assertEqual(self._compose(question, headers=self.employee).json()["code"],
                         "COMPOSITION_NO_EVIDENCE")
        manager_item = self._compose(question, headers=self.manager).json()
        self.assertEqual(set(manager_item["source_ids"]), {"3", "4"})
        self.assertEqual(self.model.calls, [])

    def test_request_id_survives_repository_reopen(self):
        subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifacts.sqlite3"
            first = ArtifactRepository(path)
            item = first.create_from_question(subject, self.documents,
                                              question="差旅报销审批流程", request_id="answer-doc-001")
            first._db.close()
            reopened = ArtifactRepository(path)
            replay = reopened.create_from_question(subject, self.documents,
                                                   question="差旅报销审批流程", request_id="answer-doc-001")
            self.assertEqual(replay["id"], item["id"])
            self.assertFalse(replay["composition_created"])
            reopened._db.close()

    def test_revocation_and_version_change_block_replay_and_model(self):
        item = self._compose().json()
        self.assertEqual(self.client.put("/api/v1/admin/documents/1/deny", headers=self.admin,
                                         json={"user_id": "employee-001", "denied": True}).status_code, 204)
        self.assertEqual(self._compose().json()["code"], "COMPOSITION_NO_EVIDENCE")
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{item['id']}/collaborate",
                                          headers=self.employee, json={"draft_token": item["draft_token"],
                                                                       "request_id": "answer-doc-001"}).status_code, 404)
        self.assertEqual(self.model.calls, [])
        self.documents.set_denied("demo-org", "1", "employee-001", False)
        self.documents._db.execute("UPDATE documents SET version = 'v99' WHERE document_id = '1'")
        self.documents._db.commit()
        replay = self._compose()
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["code"], "SOURCE_VERSION_CHANGED")


if __name__ == "__main__":
    unittest.main()
