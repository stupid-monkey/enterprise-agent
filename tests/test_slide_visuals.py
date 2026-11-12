import io
import unittest

import httpx
from fastapi.testclient import TestClient
from docx import Document
from docx.shared import Inches
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.api.artifacts import ArtifactRepository
from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.api.slide_visuals import UnsplashPhotoProvider, embedded_visuals, visual_theme
from app.security.policy import Classification, Subject, UserRole, Visibility


def sample_image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1200, 800), "#80b3a2").save(output, "JPEG")
    return output.getvalue()


def source_document() -> bytes:
    document = Document()
    document.add_paragraph("系统架构图展示 master、worker 与数据库组件。")
    document.add_picture(io.BytesIO(sample_image()), width=Inches(5))
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


class FakePhotoProvider:
    def __init__(self, fail=False):
        self.queries = []
        self.fail = fail

    def find(self, query, excluded_ids):
        self.queries.append(query)
        if self.fail:
            raise httpx.ConnectError("offline")
        photo_id = f"photo-{len(self.queries)}"
        return {"image": sample_image(), "photo_id": photo_id,
                "photographer": "Test Photographer", "page_url": f"https://unsplash.com/photos/{photo_id}",
                "provider": "Unsplash", "license_url": "https://unsplash.com/license"}


class SlideVisualTests(unittest.TestCase):
    def setUp(self):
        self.docs = DocumentRepository(":memory:", seed_demo=True)
        self.artifacts = ArtifactRepository(":memory:")
        self.provider = FakePhotoProvider()
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject(
                "employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("manager", hash_password("password"), Subject(
                "manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL,
                role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("admin", hash_password("password"), Subject(
                "admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED,
                role=UserRole.SYSTEM_ADMIN)),
        ]
        self.client = TestClient(create_app(LocalAuth(accounts), self.docs,
                                            artifacts=self.artifacts, slide_photo_provider=self.provider))
        self.employee = self._login("employee")
        self.manager = self._login("manager")
        self.admin = self._login("admin")

    def _login(self, username):
        token = self.client.post("/api/v1/auth/login", json={
            "username": username, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def _create(self):
        response = self.client.post("/api/v1/artifacts", headers=self.employee, json={
            "kind": "pptx", "title": "差旅报销总结", "goal": "部门例会", "source_ids": ["1"],
            "outline": ["报销概览", "审批流程", "数据分析", "文件管理", "风险与待办", "下阶段计划"],
        })
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_online_visuals_are_private_persisted_and_embedded(self):
        item = self._create()
        path = f"/api/v1/artifacts/{item['id']}/slide-visuals"
        self.assertEqual(self.client.post(path, headers=self.admin,
                                          json={"draft_token": item["draft_token"]}).status_code, 404)
        matched = self.client.post(path, headers=self.employee,
                                   json={"draft_token": item["draft_token"]})
        self.assertEqual(matched.status_code, 200)
        visuals = matched.json()["slide_visuals"]
        self.assertEqual([row["page_index"] for row in visuals], [0, 1, 3, 5])
        self.assertTrue(all(row["source"] == "online" for row in visuals))
        self.assertTrue(all(query.isascii() and "报销" not in query for query in self.provider.queries))
        self.assertTrue(all("image" not in row for row in visuals))
        image_path = f"{path}/0/image"
        self.assertEqual(self.client.get(image_path, headers=self.employee).content, sample_image())
        self.assertEqual(self.client.get(image_path, headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get(image_path, headers=self.employee).headers["cache-control"],
                         "private, no-store")

        notes = self.client.post(f"/api/v1/artifacts/{item['id']}/notes", headers=self.employee, json={
            "draft_token": item["draft_token"], "notes": [
                {"heading": heading, "text": "人工核对" if index == 0 else ""}
                for index, heading in enumerate(item["outline"])]}).json()
        self.assertEqual(len(notes["slide_visuals"]), 4)
        confirmed = self.client.post(f"/api/v1/artifacts/{item['id']}/confirm", headers=self.employee,
                                     json={"outline": item["outline"], "draft_token": notes["draft_token"]})
        self.assertEqual(confirmed.status_code, 200)
        payload = self.client.get(f"/api/v1/artifacts/{item['id']}/download", headers=self.employee).content
        deck = Presentation(io.BytesIO(payload))
        self.assertTrue(any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE and shape.image.blob == sample_image()
                            for shape in deck.slides[0].shapes))
        self.assertTrue(any("Test Photographer / Unsplash" in shape.text
                            for shape in deck.slides[0].shapes if shape.has_text_frame))
        self.client.put("/api/v1/admin/documents/1/deny", headers=self.admin,
                        json={"user_id": "employee-001", "denied": True})
        self.assertEqual(self.client.get(image_path, headers=self.employee).status_code, 404)

    def test_local_fallback_and_changed_draft_hide_old_visuals(self):
        self.provider.fail = True
        item = self._create()
        path = f"/api/v1/artifacts/{item['id']}/slide-visuals"
        result = self.client.post(path, headers=self.employee,
                                  json={"draft_token": item["draft_token"]}).json()
        self.assertEqual(len(self.provider.queries), 1)
        self.assertTrue(all(row["source"] == "local" for row in result["slide_visuals"]))
        self.assertEqual(len({row["asset_name"] for row in result["slide_visuals"]}), 4)
        self.assertEqual(self.client.get(f"{path}/0/image", headers=self.employee).status_code, 404)
        self.assertEqual(result["slide_visuals"][1]["category"], "planning")
        changed = self.client.post(f"/api/v1/artifacts/{item['id']}/preview", headers=self.employee,
                                   json={"outline": ["安全权限"] + item["outline"][1:]}).json()
        self.assertEqual(changed["slide_visuals"], [])
        self.assertEqual(self.client.post(path, headers=self.employee,
                                          json={"draft_token": item["draft_token"]}).status_code, 409)

    def test_authorized_source_image_precedes_online_and_is_revocable(self):
        subject = Subject("manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL,
                          role=UserRole.DEPARTMENT_MANAGER)
        content = source_document()
        document_id = self.docs.create_upload(
            subject, title="系统架构", filename="architecture.docx", file_type="DOCX",
            area="技术资料", visibility=Visibility.ORGANIZATION, content=content,
            extracted_text="系统架构图展示 master、worker 与数据库组件。")
        self.docs.publish(subject, document_id)
        created = self.client.post("/api/v1/artifacts", headers=self.manager, json={
            "kind": "pptx", "title": "系统架构", "goal": "部门例会", "source_ids": [document_id],
            "outline": ["系统架构图", "系统架构图"],
        })
        self.assertEqual(created.status_code, 201)
        item = created.json()
        path = f"/api/v1/artifacts/{item['id']}/slide-visuals"
        matched = self.client.post(path, headers=self.manager,
                                   json={"draft_token": item["draft_token"]})
        self.assertEqual(matched.status_code, 200)
        visuals = matched.json()["slide_visuals"]
        self.assertEqual(visuals[0]["source"], "document")
        self.assertEqual(visuals[0]["source_document_id"], document_id)
        self.assertEqual(visuals[0]["source_location"], "段落 2")
        self.assertNotEqual(visuals[1]["source"], "document")
        self.assertEqual(len(self.provider.queries), 1)
        image_path = f"{path}/0/image"
        self.assertEqual(self.client.get(image_path, headers=self.manager).status_code, 200)
        self.assertEqual(self.client.get(image_path, headers=self.admin).status_code, 404)
        confirmed = self.client.post(f"/api/v1/artifacts/{item['id']}/confirm", headers=self.manager,
                                     json={"outline": item["outline"], "draft_token": item["draft_token"]})
        self.assertEqual(confirmed.status_code, 200)
        payload = self.client.get(f"/api/v1/artifacts/{item['id']}/download", headers=self.manager).content
        deck = Presentation(io.BytesIO(payload))
        self.assertTrue(any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE
                            for shape in deck.slides[0].shapes))
        self.docs.set_denied("demo-org", document_id, subject.user_id, True)
        self.assertEqual(self.client.get(image_path, headers=self.manager).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{item['id']}/download",
                                         headers=self.manager).status_code, 404)

    def test_embedded_visuals_reject_small_and_unsupported_images(self):
        self.assertEqual(embedded_visuals("TXT", b"text"), [])
        self.assertEqual(len(embedded_visuals("DOCX", source_document())), 1)
        document = Document()
        small = io.BytesIO()
        Image.new("RGB", (80, 80), "red").save(small, "PNG")
        document.add_picture(io.BytesIO(small.getvalue()))
        output = io.BytesIO()
        document.save(output)
        self.assertEqual(embedded_visuals("DOCX", output.getvalue()), [])

        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(0, 0, 2000000, 500000).text = "系统架构图"
        slide.shapes.add_picture(io.BytesIO(sample_image()), 0, 500000, 4000000, 2500000)
        output = io.BytesIO()
        deck.save(output)
        visuals = embedded_visuals("PPTX", output.getvalue())
        self.assertEqual(len(visuals), 1)
        self.assertIn("系统架构图", visuals[0]["context"])

    def test_source_version_change_hides_old_visual(self):
        subject = Subject("manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL,
                          role=UserRole.DEPARTMENT_MANAGER)
        content = source_document()
        document_id = self.docs.create_upload(
            subject, title="系统架构", filename="architecture.docx", file_type="DOCX",
            area="技术资料", visibility=Visibility.ORGANIZATION, content=content,
            extracted_text="系统架构图展示 master、worker 与数据库组件。")
        self.assertTrue(self.docs.publish(subject, document_id))
        item = self.client.post("/api/v1/artifacts", headers=self.manager, json={
            "kind": "pptx", "title": "系统架构", "goal": "部门例会", "source_ids": [document_id],
            "outline": ["系统架构图", "启动流程图"],
        }).json()
        path = f"/api/v1/artifacts/{item['id']}/slide-visuals"
        self.assertEqual(self.client.post(path, headers=self.manager,
                                          json={"draft_token": item["draft_token"]}).json()
                         ["slide_visuals"][0]["source"], "document")
        version = self.docs.create_revision(subject, document_id, filename="new.docx",
                                            file_type="DOCX", content=content,
                                            extracted_text="新版本的系统架构图。")
        self.assertEqual(self.docs.publish_revision(subject, document_id), version)
        stale = self.client.get(f"/api/v1/artifacts/{item['id']}", headers=self.manager).json()
        self.assertEqual(stale["source_status"], "stale")
        self.assertEqual(stale["slide_visuals"], [])
        self.assertEqual(self.client.get(f"{path}/0/image", headers=self.manager).status_code, 404)

    def test_provider_accepts_only_approved_query_and_image_host(self):
        image = sample_image()
        calls = []

        def handle(request):
            calls.append(str(request.url))
            if request.url.host == "api.unsplash.com":
                return httpx.Response(200, json={"results": [
                    {"id": "bad", "width": 1200, "height": 800,
                     "urls": {"regular": "https://internal.example.com/secret.jpg"},
                     "links": {"html": "https://unsplash.com/photos/bad"}},
                    {"id": "good", "width": 1200, "height": 800,
                     "urls": {"regular": "https://images.unsplash.com/photo-good"},
                     "links": {"html": "https://unsplash.com/photos/good"},
                     "user": {"name": "Photographer"}},
                ]})
            return httpx.Response(200, content=image, headers={"content-type": "image/jpeg"})

        provider = UnsplashPhotoProvider("test-key", httpx.Client(transport=httpx.MockTransport(handle)))
        with self.assertRaises(ValueError):
            provider.find("private project name", set())
        query = visual_theme("产品", {"heading": "权限治理", "text": ""}).query
        result = provider.find(query, set())
        self.assertEqual(result["photo_id"], "good")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("private project name" not in call for call in calls))


if __name__ == "__main__":
    unittest.main()
