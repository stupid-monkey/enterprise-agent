import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.api.workflow_runs import WorkflowRunRepository
from app.security.policy import Classification, Subject, UserRole


class WorkflowRunApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "runs.sqlite3"
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        self.manager = Subject("manager-001", "demo-org", frozenset({"product"}),
                               Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)
        self.employee = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        self.auth = LocalAuth([
            LocalAccount("manager", hash_password("password"), self.manager),
            LocalAccount("employee", hash_password("password"), self.employee),
        ])
        self.runs = WorkflowRunRepository(self.path)
        self.client = TestClient(create_app(self.auth, self.documents, workflow_runs=self.runs))

    def tearDown(self):
        self.runs.close()
        self.documents.close()
        self.directory.cleanup()

    def headers(self, username):
        response = self.client.post("/api/v1/auth/login", json={"username": username, "password": "password"})
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def start(self, headers, question="产品发布前有哪些检查项？"):
        response = self.client.post("/api/v1/knowledge-runs", headers=headers, json={"question": question})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "pending")
        return response.json()["id"]

    def test_owner_bound_checkpoint_survives_app_restart(self):
        manager = self.headers("manager")
        employee = self.headers("employee")
        run_id = self.start(manager)
        self.assertEqual(self.client.get(f"/api/v1/knowledge-runs/{run_id}", headers=manager).json(),
                         {"id": run_id, "status": "pending"})
        self.assertEqual(self.client.get(f"/api/v1/knowledge-runs/{run_id}", headers=employee).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=employee).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/knowledge-runs/no-such-run/resume", headers=manager).status_code, 404)

        self.runs.close()
        self.runs = WorkflowRunRepository(self.path)
        self.client = TestClient(create_app(self.auth, self.documents, workflow_runs=self.runs))
        answer = self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=manager)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["status"], "evidence_ready")
        self.assertEqual(answer.json()["citations"][0]["document_id"], "5")
        self.assertEqual(self.client.get(f"/api/v1/knowledge-runs/{run_id}", headers=manager).json()["status"], "completed")
        self.assertEqual(self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=manager).json(),
                         answer.json())

    def test_revocation_and_version_change_are_checked_on_resume(self):
        manager = self.headers("manager")
        revoked = self.start(manager)
        self.documents.set_denied("demo-org", "5", self.manager.user_id, True)
        answer = self.client.post(f"/api/v1/knowledge-runs/{revoked}/resume", headers=manager)
        self.assertEqual(answer.json()["status"], "no_evidence")
        self.assertEqual(answer.json()["citations"], [])
        self.documents.set_denied("demo-org", "5", self.manager.user_id, False)

        stale = self.start(manager)
        with self.documents._lock, self.documents._db:
            self.documents._db.execute("UPDATE documents SET version = 'v2.0' WHERE document_id = '5'")
        answer = self.client.post(f"/api/v1/knowledge-runs/{stale}/resume", headers=manager)
        self.assertEqual(answer.json()["status"], "no_evidence")
        self.assertEqual(answer.json()["citations"], [])

    def test_employee_run_cannot_collect_manager_only_source(self):
        employee = self.headers("employee")
        run_id = self.start(employee)
        answer = self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=employee)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["status"], "no_evidence")
        self.assertEqual(answer.json()["citations"], [])

    def test_completed_run_rechecks_sources_before_replaying_answer(self):
        manager = self.headers("manager")
        run_id = self.start(manager)
        first = self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=manager)
        self.assertEqual(first.json()["status"], "evidence_ready")
        self.documents.set_denied("demo-org", "5", self.manager.user_id, True)
        replay = self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=manager)
        self.assertEqual(replay.json()["status"], "no_evidence")
        self.assertEqual(replay.json()["citations"], [])

    def test_resume_recovers_record_without_initial_checkpoint(self):
        manager = self.headers("manager")
        run_id = uuid4().hex
        with self.runs._lock, self.runs._db:
            self.runs._db.execute("""INSERT INTO workflow_runs
                (id, org_id, owner_id, question, area, status) VALUES (?, ?, ?, ?, ?, ?)""",
                                  (run_id, self.manager.org_id, self.manager.user_id,
                                   "产品发布前有哪些检查项？", None, "pending"))
        answer = self.client.post(f"/api/v1/knowledge-runs/{run_id}/resume", headers=manager)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["citations"][0]["document_id"], "5")


if __name__ == "__main__":
    unittest.main()
