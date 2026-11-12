import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.api.audit import AuditRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.audit_path = Path(self.directory.name) / "audit.sqlite3"
        self.audit = AuditRepository(self.audit_path)
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("admin", hash_password("password"), Subject(
                "admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED,
                role=UserRole.SYSTEM_ADMIN)),
            LocalAccount("other-admin", hash_password("password"), Subject(
                "other-admin-001", "other-org", frozenset({"it"}), Classification.RESTRICTED,
                role=UserRole.SYSTEM_ADMIN)),
        ]
        self.client = TestClient(create_app(LocalAuth(accounts), self.documents, audit=self.audit))

    def tearDown(self):
        self.audit.close()
        self.documents.close()
        self.directory.cleanup()

    def login(self, username):
        response = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "password"
        })
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def test_admin_listing_is_authenticated_and_organization_scoped(self):
        endpoint = "/api/v1/admin/audit-events"
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        employee = self.login("employee")
        self.assertEqual(self.client.get(endpoint, headers=employee).status_code, 403)
        other = self.login("other-admin")
        admin = self.login("admin")
        other_events = self.client.get(endpoint, headers=other).json()
        self.assertEqual({event["actor_id"] for event in other_events}, {"other-admin-001"})
        events = self.client.get(endpoint, headers=admin).json()
        self.assertEqual({event["actor_id"] for event in events}, {"employee-001", "admin-001"})
        self.assertEqual({event["org_id"] for event in events}, {"demo-org"})
        self.assertNotIn("password", str(events))

    def test_events_capture_result_resource_and_response_trace(self):
        employee = self.login("employee")
        admin = self.login("admin")
        denied = self.client.post("/api/v1/documents/1/publish", headers=employee)
        self.assertEqual(denied.status_code, 404)
        allowed = self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                                  json={"user_id": "employee-001", "denied": True})
        self.assertEqual(allowed.status_code, 204)
        events = self.client.get("/api/v1/admin/audit-events", headers=admin).json()
        by_trace = {event["trace_id"]: event for event in events}
        self.assertEqual(by_trace[denied.headers["X-Trace-Id"]]["outcome"], "denied")
        self.assertEqual(by_trace[denied.headers["X-Trace-Id"]]["resource_id"], "1")
        self.assertEqual(by_trace[allowed.headers["X-Trace-Id"]]["action"], "document.deny")
        self.assertEqual(by_trace[allowed.headers["X-Trace-Id"]]["outcome"], "success")

    def test_records_survive_repository_reopen(self):
        self.login("employee")
        self.audit.close()
        reopened = AuditRepository(self.audit_path)
        try:
            events = reopened.list_for_org("demo-org")
            self.assertEqual(events[0]["action"], "auth.login")
            self.assertEqual(reopened.list_for_org("other-org"), [])
        finally:
            self.audit = reopened

    def test_artifact_lifecycle_is_audited_without_document_content(self):
        employee = self.login("employee")
        admin = self.login("admin")
        created = self.client.post("/api/v1/artifacts", headers=employee, json={
            "kind": "docx", "title": "差旅记录", "goal": "整理审批", "source_ids": ["1"],
            "outline": ["审批流程"],
        })
        self.assertEqual(created.status_code, 201)
        artifact_id = created.json()["id"]
        confirmed = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                     json={"outline": ["审批流程"]})
        self.assertEqual(confirmed.status_code, 200)
        downloaded = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=admin).status_code, 404)
        events = self.client.get("/api/v1/admin/audit-events", headers=admin).json()
        actions = [event["action"] for event in events]
        self.assertIn("artifact.create", actions)
        self.assertIn("artifact.confirm", actions)
        self.assertEqual([(event["action"], event["outcome"]) for event in events
                          if event["action"] == "artifact.download"],
                         [("artifact.download", "denied"), ("artifact.download", "success")])
        self.assertNotIn("整理审批", str(events))


if __name__ == "__main__":
    unittest.main()
