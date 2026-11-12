import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.security.policy import Classification, Subject, UserRole


class DocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.repo = DocumentRepository(":memory:", seed_demo=True)
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("manager", hash_password("password"), Subject("manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("admin", hash_password("password"), Subject("admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED, role=UserRole.SYSTEM_ADMIN)),
            LocalAccount("outsider", hash_password("password"), Subject("outside-001", "other-org", frozenset({"product"}), Classification.RESTRICTED)),
        ]
        self.client = TestClient(create_app(LocalAuth(accounts), self.repo))

    def tearDown(self):
        self.repo.close()

    def headers(self, username):
        token = self.client.post("/api/v1/auth/login", json={"username": username, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_list_and_detail_enforce_document_policy(self):
        self.assertEqual(self.client.get("/api/v1/documents").status_code, 401)
        employee = self.headers("employee")
        manager = self.headers("manager")
        admin = self.headers("admin")
        outsider = self.headers("outsider")
        self.assertEqual({item["id"] for item in self.client.get("/api/v1/documents", headers=employee).json()}, {"1", "2"})
        self.assertEqual({item["id"] for item in self.client.get("/api/v1/documents", headers=manager).json()}, {"1", "2", "3", "4", "5", "6"})
        self.assertEqual(self.client.get("/api/v1/documents", headers=admin).json(), [])
        self.assertEqual(self.client.get("/api/v1/documents", headers=outsider).json(), [])
        self.assertNotIn("excerpt", self.client.get("/api/v1/documents", headers=manager).text)
        self.assertIn("excerpt", self.client.get("/api/v1/documents/5", headers=manager).json())
        for headers in (employee, admin, outsider):
            self.assertEqual(self.client.get("/api/v1/documents/5", headers=headers).status_code, 404)

    def test_revocation_applies_to_list_and_detail_without_restart(self):
        employee = self.headers("employee")
        admin = self.headers("admin")
        uri = "/api/v1/admin/documents/1/deny"
        body = {"user_id": "employee-001", "denied": True}
        self.assertEqual(self.client.put(uri, headers=employee, json=body).status_code, 403)
        self.assertEqual(self.client.put(uri, headers=admin, json={"user_id": "outside-001", "denied": True}).status_code, 404)
        self.assertEqual(self.client.put(uri, headers=admin, json=body).status_code, 204)
        self.assertEqual({item["id"] for item in self.client.get("/api/v1/documents", headers=employee).json()}, {"2"})
        self.assertEqual(self.client.get("/api/v1/documents/1", headers=employee).status_code, 404)
        self.assertEqual(self.client.put(uri, headers=admin, json={**body, "denied": False}).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/documents/1", headers=employee).status_code, 200)

    def test_text_preview_is_bounded_and_rechecks_access(self):
        employee = self.headers("employee")
        manager = self.headers("manager")
        admin = self.headers("admin")
        self.assertEqual(self.client.get("/api/v1/documents/1/preview").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/documents/5/preview", headers=employee).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/documents/1/preview", headers=admin).status_code, 404)
        content = "试点说明。" * 1400
        created = self.client.post("/api/v1/documents/upload", headers=manager,
            data={"title": "长文预览", "area": "产品资料", "visibility": "内部公开"},
            files={"file": ("pilot.txt", content.encode(), "text/plain")})
        self.assertEqual(created.status_code, 201)
        document_id = created.json()["id"]
        uri = f"/api/v1/documents/{document_id}/preview"
        self.assertEqual(self.client.get(uri, headers=employee).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish", headers=manager).status_code, 204)
        preview = self.client.get(uri, headers=employee)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["preview_text"], content[:6000])
        self.assertTrue(preview.json()["truncated"])
        self.assertEqual(preview.headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.put(f"/api/v1/admin/documents/{document_id}/deny", headers=admin,
                                         json={"user_id": "employee-001", "denied": True}).status_code, 204)
        self.assertEqual(self.client.get(uri, headers=employee).status_code, 404)

    def test_revocation_survives_repository_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            first = DocumentRepository(path, seed_demo=True)
            self.assertTrue(first.set_denied("demo-org", "1", "employee-001", True))
            first.close()
            second = DocumentRepository(path, seed_demo=True)
            subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
            self.assertIsNone(second.get_visible(subject, "1"))
            second.close()

    def test_original_download_tracks_publication_revision_and_revocation(self):
        employee = self.headers("employee")
        manager = self.headers("manager")
        admin = self.headers("admin")
        outsider = self.headers("outsider")
        created = self.client.post("/api/v1/documents/upload", headers=manager,
            data={"title": "试点说明", "area": "产品资料", "visibility": "内部公开"},
            files={"file": ("pilot.txt", "十一月启动试点。".encode(), "text/plain")})
        self.assertEqual(created.status_code, 201)
        document_id = created.json()["id"]
        uri = f"/api/v1/documents/{document_id}/original"
        self.assertEqual(self.client.get(uri).status_code, 401)
        self.assertEqual(self.client.get(uri, headers=employee).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish", headers=manager).status_code, 204)
        detail = self.client.get(f"/api/v1/documents/{document_id}", headers=employee).json()
        self.assertTrue(detail["has_original"])
        self.assertEqual(self.client.get(uri, headers=admin).status_code, 404)
        self.assertEqual(self.client.get(uri, headers=outsider).status_code, 404)
        original = self.client.get(uri, headers=employee)
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.content, "十一月启动试点。".encode())
        self.assertEqual(original.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("pilot.txt", original.headers["content-disposition"])
        self.assertEqual(original.headers["cache-control"], "private, no-store")
        self.assertEqual(original.headers["x-content-type-options"], "nosniff")

        revision_uri = f"/api/v1/documents/{document_id}/versions"
        updated = self.client.post(revision_uri, headers=manager,
            files={"file": ("pilot-v2.txt", "十二月启动试点。".encode(), "text/plain")})
        self.assertEqual(updated.status_code, 201)
        self.assertEqual(self.client.get(uri, headers=employee).content, original.content)
        self.assertEqual(self.client.post(f"{revision_uri}/publish", headers=manager).status_code, 204)
        stale = self.client.get(uri, headers=employee, params={"version": "v1.0"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "DOCUMENT_VERSION_CHANGED")
        latest = self.client.get(uri, headers=employee)
        self.assertEqual(latest.content, "十二月启动试点。".encode())
        self.assertIn("pilot-v2.txt", latest.headers["content-disposition"])
        self.assertEqual(self.client.put(f"/api/v1/admin/documents/{document_id}/deny", headers=admin,
                                         json={"user_id": "employee-001", "denied": True}).status_code, 204)
        denied = self.client.get(uri, headers=employee)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.json()["code"], "DOCUMENT_NOT_FOUND")
        self.assertEqual(self.client.get(uri, headers=manager).content, latest.content)
        events = self.client.get("/api/v1/admin/audit-events", headers=admin).json()
        by_trace = {event["trace_id"]: event for event in events}
        self.assertEqual(by_trace[original.headers["x-trace-id"]]["action"], "document.original.download")
        self.assertEqual(by_trace[original.headers["x-trace-id"]]["outcome"], "success")
        self.assertEqual(by_trace[denied.headers["x-trace-id"]]["outcome"], "denied")
        self.assertNotIn("十二月", str(events))

    def test_demo_document_has_no_original_and_filename_is_sanitized(self):
        employee = self.headers("employee")
        manager = self.headers("manager")
        listing = self.client.get("/api/v1/documents", headers=employee).json()
        self.assertTrue(all(item["has_original"] is False for item in listing))
        self.assertEqual(self.client.get("/api/v1/documents/1/original", headers=employee).status_code, 404)
        created = self.client.post("/api/v1/documents/upload", headers=manager,
            data={"title": "安全文件名", "area": "制度规范", "visibility": "内部公开"},
            files={"file": ("../secret.txt", b"Safe source text.", "text/plain")})
        self.assertEqual(created.status_code, 201)
        document_id = created.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish", headers=manager).status_code, 204)
        response = self.client.get(f"/api/v1/documents/{document_id}/original", headers=employee)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-disposition"], "attachment; filename*=UTF-8''secret.txt")

    def test_document_revision_review_publish_and_artifact_invalidation(self):
        employee = self.headers("employee")
        manager = self.headers("manager")
        admin = self.headers("admin")
        created = self.client.post("/api/v1/documents/upload", headers=manager,
            data={"title": "产品试点计划", "area": "产品资料", "visibility": "内部公开"},
            files={"file": ("plan.txt", "产品试点计划在十一月启动。".encode(), "text/plain")})
        self.assertEqual(created.status_code, 201)
        document_id = created.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish", headers=manager).status_code, 204)
        artifact = self.client.post("/api/v1/artifacts", headers=manager, json={
            "kind": "docx", "title": "试点简报", "goal": "总结试点", "source_ids": [document_id],
            "outline": ["计划"],
        })
        self.assertEqual(artifact.status_code, 201)
        artifact_id = artifact.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=manager,
                                          json={"outline": ["计划"]}).status_code, 200)

        uri = f"/api/v1/documents/{document_id}/versions"
        new_file = {"file": ("plan-v2.txt", "产品试点计划改为十二月启动。".encode(), "text/plain")}
        self.assertEqual(self.client.post(uri, headers=employee, files=new_file).status_code, 404)
        self.assertEqual(self.client.post(uri, headers=admin, files=new_file).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/documents/1/versions", headers=manager,
                                          files=new_file).status_code, 404)
        pending = self.client.post(uri, headers=manager, files=new_file)
        self.assertEqual((pending.status_code, pending.json()["version"]), (201, "v1.1"))
        self.assertEqual(self.client.post(uri, headers=manager, files=new_file).json()["code"], "REVISION_PENDING")
        self.assertEqual(self.client.get("/api/v1/documents/versions/pending", headers=employee).json(), [])
        self.assertEqual(len(self.client.get("/api/v1/documents/versions/pending", headers=manager).json()), 1)
        self.assertIn("十一月", self.client.get(f"/api/v1/documents/{document_id}", headers=employee).json()["excerpt"])
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=manager).status_code, 200)
        self.assertEqual(self.client.post(f"{uri}/publish", headers=employee).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/admin/documents/{document_id}/deny", headers=admin,
                                         json={"user_id": "manager-001", "denied": True}).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/documents/versions/pending", headers=manager).json(), [])
        self.assertEqual(self.client.post(uri, headers=manager, files=new_file).status_code, 404)
        self.assertEqual(self.client.post(f"{uri}/publish", headers=manager).status_code, 404)
        self.assertEqual(self.client.put(f"/api/v1/admin/documents/{document_id}/deny", headers=admin,
                                         json={"user_id": "manager-001", "denied": False}).status_code, 204)
        self.assertEqual(self.client.post(f"{uri}/publish", headers=manager).status_code, 204)
        self.assertEqual(self.client.post(f"{uri}/publish", headers=manager).status_code, 404)
        detail = self.client.get(f"/api/v1/documents/{document_id}", headers=employee).json()
        self.assertEqual(detail["version"], "v1.1")
        self.assertIn("十二月", detail["excerpt"])
        self.assertNotIn("十一月", detail["excerpt"])
        reader = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        old_hits = [hit for hit in self.repo.search_visible(reader, "十一月启动") if hit.document.document_id == document_id]
        new_hits = [hit for hit in self.repo.search_visible(reader, "十二月启动") if hit.document.document_id == document_id]
        self.assertTrue(new_hits)
        self.assertTrue(all("十一月" not in hit.text for hit in old_hits + new_hits))
        self.assertTrue(any("十二月" in hit.text for hit in new_hits))
        indexed = self.repo._db.execute("SELECT text FROM document_segments WHERE document_id = ?",
                                        (document_id,)).fetchall()
        self.assertTrue(indexed)
        self.assertTrue(all("十一月" not in row["text"] for row in indexed))
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=manager).status_code, 409)
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=manager).json()["source_status"], "stale")
        third_file = {"file": ("plan-v3.txt", "产品试点计划改为一月启动。".encode(), "text/plain")}
        self.assertEqual(self.client.post(uri, headers=manager, files=third_file).json()["version"], "v1.2")
        self.assertEqual(self.client.post(f"{uri}/publish", headers=manager).status_code, 204)
        statuses = self.repo._db.execute("SELECT version, status FROM document_revisions WHERE document_id = ? ORDER BY version",
                                         (document_id,)).fetchall()
        self.assertEqual([(row["version"], row["status"]) for row in statuses],
                         [("v1.0", "archived"), ("v1.1", "archived"), ("v1.2", "published")])
        archived = self.repo._db.execute("""SELECT content FROM document_revisions
            WHERE document_id = ? AND version = 'v1.0'""", (document_id,)).fetchone()
        self.assertIn("十一月", archived["content"].decode())


if __name__ == "__main__":
    unittest.main()
