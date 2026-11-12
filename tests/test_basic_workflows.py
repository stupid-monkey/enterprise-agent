import io
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from docx import Document
from fastapi.testclient import TestClient
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfWriter

from app.api.auth import LocalAccount, LocalAuth, hash_password
from app.api.artifacts import ArtifactRepository, make_file
from app.api.documents import DocumentRepository
from app.api.main import create_app
from app.api.content import extract_text, relevant_sentences
from app.api.workflow_answers import run_knowledge_workflow
from app.security.policy import Classification, Subject, UserRole


class BasicWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository(":memory:", seed_demo=True)
        accounts = [
            LocalAccount("employee", hash_password("password"), Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)),
            LocalAccount("manager", hash_password("password"), Subject("manager-001", "demo-org", frozenset({"product"}), Classification.CONFIDENTIAL, role=UserRole.DEPARTMENT_MANAGER)),
            LocalAccount("admin", hash_password("password"), Subject("admin-001", "demo-org", frozenset({"it"}), Classification.RESTRICTED, role=UserRole.SYSTEM_ADMIN)),
        ]
        self.client = TestClient(create_app(LocalAuth(accounts), self.documents))

    def tearDown(self):
        self.documents.close()

    def headers(self, username):
        token = self.client.post("/api/v1/auth/login", json={"username": username, "password": "password"}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_supported_file_parsing_and_invalid_archives(self):
        doc = Document()
        doc.add_paragraph("审批后提交财务复核。")
        buffer = io.BytesIO()
        doc.save(buffer)
        self.assertIn("审批后", extract_text("policy.docx", buffer.getvalue())[1])

        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = "产品发布检查"
        buffer = io.BytesIO()
        deck.save(buffer)
        self.assertIn("产品发布", extract_text("review.pptx", buffer.getvalue())[1])

        writer = PdfWriter()
        writer.add_blank_page(width=300, height=300)
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaisesRegex(ValueError, "UNREADABLE_FILE"):
            extract_text("scan.pdf", buffer.getvalue())
        for suffix in ("docx", "pptx", "pdf"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "INVALID_FILE"):
                extract_text(f"bad.{suffix}", b"not a valid document")

    def test_generic_question_words_do_not_create_evidence(self):
        self.assertEqual(relevant_sentences("这个资料是什么内容？", "资料内容需要经过部门审核。"), [])
        self.assertTrue(relevant_sentences("产品试点什么时候启动？", "产品试点将在十一月启动。"))

    def test_upload_publish_and_grounded_answer(self):
        employee = self.headers("employee")
        manager = self.headers("manager")
        upload = lambda headers: self.client.post(
            "/api/v1/documents/upload", headers=headers,
            data={"title": "产品试点安排", "area": "产品资料", "visibility": "部门受限"},
            files={"file": ("plan.txt", "产品试点将在十一月启动。负责人是林晓宇。".encode(), "text/plain")},
        )
        self.assertEqual(upload(employee).status_code, 403)
        created = upload(manager)
        self.assertEqual(created.status_code, 201)
        document_id = created.json()["id"]
        self.assertEqual(self.client.get(f"/api/v1/documents/{document_id}", headers=manager).status_code, 404)
        self.assertNotIn(document_id, [item["document_id"] for item in self.client.post(
            "/api/v1/answers", headers=manager, json={"question": "产品试点什么时候启动？"}
        ).json()["citations"]])
        self.assertIn(document_id, [item["id"] for item in self.client.get("/api/v1/documents/pending/review", headers=manager).json()])
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish", headers=employee).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/documents/{document_id}/publish", headers=manager).status_code, 204)
        answer = self.client.post("/api/v1/answers", headers=manager, json={"question": "产品试点什么时候启动？", "area": "产品资料"}).json()
        self.assertEqual(answer["status"], "evidence_ready")
        self.assertTrue(any(item["document_id"] == document_id for item in answer["citations"]))
        citation = next(item for item in answer["citations"] if item["document_id"] == document_id)
        self.assertIn("片段", citation["location"])
        self.assertIn("产品试点", citation["excerpt"])
        self.assertNotIn(document_id, [item["document_id"] for item in self.client.post(
            "/api/v1/answers", headers=employee, json={"question": "产品试点什么时候启动？"}
        ).json()["citations"]])
        no_evidence = self.client.post("/api/v1/answers", headers=employee, json={"question": "病假证明怎么办？"}).json()
        self.assertEqual(no_evidence["status"], "no_evidence")
        self.assertEqual(no_evidence["citations"], [])

    def test_slide_outline_uses_selected_markdown_evidence(self):
        manager = self.headers("manager")
        employee = self.headers("employee")
        content = ("# 特性\n\n## 简单易用\n\n- **可视化 DAG**: 拖拽定义工作流。\n\n"
                   "- **模块化操作**: 支持定制和维护。\n\n## 丰富的场景\n\n"
                   "- **多种任务类型**: 支持 Shell 和 SQL。\n\n"
                   "- **工作流操作**: 可以暂停和恢复。\n\n## 高可靠性\n\n"
                   "- **容错能力**: 支持任务队列。\n\n## 高扩展性\n\n"
                   "- **多租户**: 支持资源管理。")
        uploaded = self.client.post("/api/v1/documents/upload", headers=manager,
            data={"title": "工作流核心功能", "area": "产品资料", "visibility": "部门受限"},
            files={"file": ("features.md", content.encode(), "text/markdown")})
        self.assertEqual(uploaded.status_code, 201)
        source_id = uploaded.json()["id"]
        self.assertEqual(self.client.post(f"/api/v1/documents/{source_id}/publish", headers=manager).status_code, 204)
        request = {"source_ids": [source_id], "count": 8}
        self.assertEqual(self.client.post("/api/v1/artifacts/slide-outline", headers=employee,
                                          json=request).status_code, 403)
        response = self.client.post("/api/v1/artifacts/slide-outline", headers=manager, json=request)
        self.assertEqual(response.status_code, 200)
        outline = response.json()["outline"]
        self.assertEqual(len(outline), 6)
        self.assertIn("可视化 DAG", outline)
        self.assertNotIn("简单易用", outline)
        created = self.client.post("/api/v1/artifacts", headers=manager, json={
            "kind": "pptx", "title": "产品核心功能", "goal": "部门例会",
            "source_ids": [source_id], "outline": outline,
        })
        self.assertEqual(created.status_code, 201)
        self.assertTrue(all(not draft["evidence_gap"] for draft in created.json()["section_drafts"]))

    def test_search_respects_area_and_revocation(self):
        manager = self.headers("manager")
        admin = self.headers("admin")
        question = {"question": "产品发布前有哪些检查项？"}
        self.assertTrue(any(item["document_id"] == "5" for item in self.client.post(
            "/api/v1/answers", headers=manager, json=question).json()["citations"]))
        self.assertEqual(self.client.post("/api/v1/answers", headers=manager,
                                          json={**question, "area": "制度规范"}).json()["citations"], [])
        self.assertEqual(self.client.put("/api/v1/admin/documents/5/deny", headers=admin,
                                         json={"user_id": "manager-001", "denied": True}).status_code, 204)
        self.assertNotIn("5", [item["document_id"] for item in self.client.post(
            "/api/v1/answers", headers=manager, json=question).json()["citations"]])
        self.assertEqual(self.client.post("/api/v1/answers", headers=admin, json=question).json()["citations"], [])

    def test_title_question_returns_source_excerpt(self):
        manager = self.headers("manager")
        answer = self.client.post("/api/v1/answers", headers=manager,
                                  json={"question": "产品迭代路线图有哪些重点？"}).json()
        self.assertEqual(answer["status"], "evidence_ready")
        self.assertEqual(answer["citations"][0]["document_id"], "3")
        self.assertIn("本季度聚焦", answer["paragraphs"][0])

    def test_workflow_answer_enforces_subject_and_refuses_missing_evidence(self):
        employee = self.headers("employee")
        manager = self.headers("manager")
        admin = self.headers("admin")
        uri = "/api/v1/answers/workflow"
        question = {"question": "产品发布前有哪些检查项？"}
        self.assertEqual(self.client.post(uri, json=question).status_code, 401)
        manager_answer = self.client.post(uri, headers=manager, json=question).json()
        self.assertEqual(manager_answer["status"], "evidence_ready")
        self.assertEqual(manager_answer["citations"][0]["document_id"], "5")
        for headers in (employee, admin):
            answer = self.client.post(uri, headers=headers, json=question).json()
            self.assertEqual(answer["status"], "no_evidence")
            self.assertEqual(answer["citations"], [])
        self.assertEqual(self.client.post(uri, headers=manager,
                                          json={"question": "病假证明怎么办？"}).json()["status"], "no_evidence")
        self.assertEqual(self.client.post(uri, headers=manager,
                                          json={**question, "area": "制度规范"}).json()["citations"], [])
        self.assertEqual(self.client.put("/api/v1/admin/documents/5/deny", headers=admin,
                                         json={"user_id": "manager-001", "denied": True}).status_code, 204)
        self.assertEqual(self.client.post(uri, headers=manager, json=question).json()["citations"], [])

    def test_workflow_invalid_plan_fails_without_search(self):
        class InvalidPlanner:
            def plan(self, question):
                return {"action": "delete_all", "query": question}

        subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
        result = run_knowledge_workflow(subject, self.documents, "出差报销需要哪些审批？",
                                        planner=InvalidPlanner())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "INVALID_PLAN")
        self.assertEqual(result.evidence, [])

    def test_existing_catalog_rebuilds_missing_segment_index(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            first = DocumentRepository(path, seed_demo=True)
            first._db.execute("DELETE FROM document_segment_terms")
            first._db.execute("DELETE FROM document_segments")
            first._db.commit()
            first.close()
            second = DocumentRepository(path, seed_demo=True)
            subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
            self.assertTrue(second.search_visible(subject, "出差报销需要哪些审批？"))
            second.close()

    def test_document_and_presentation_generation_rechecks_sources(self):
        employee = self.headers("employee")
        admin = self.headers("admin")
        for kind in ("docx", "pptx"):
            with self.subTest(kind=kind):
                created = self.client.post("/api/v1/artifacts", headers=employee, json={
                    "kind": kind, "title": "差旅制度整理", "goal": "梳理报销审批", "source_ids": ["1"],
                    "outline": ["审批流程", "资料来源"],
                })
                self.assertEqual(created.status_code, 201)
                artifact_id = created.json()["id"]
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).status_code, 404)
                confirmed = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                             json={"outline": ["审批流程", "资料来源"]})
                self.assertEqual(confirmed.json()["status"], "completed")
                file_response = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee)
                self.assertEqual(file_response.status_code, 200)
                self.assertTrue(ZipFile(io.BytesIO(file_response.content)).testzip() is None)
                if kind == "docx":
                    self.assertIn("差旅制度整理", [p.text for p in Document(io.BytesIO(file_response.content)).paragraphs])
                else:
                    self.assertEqual(len(Presentation(io.BytesIO(file_response.content)).slides), 2)
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=admin).status_code, 404)
                self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                                  json={"outline": ["变化标题"]}).json()["outline"], ["审批流程", "资料来源"])
                self.assertEqual(self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                                                 json={"user_id": "employee-001", "denied": True}).status_code, 204)
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).status_code, 404)
                self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                                json={"user_id": "employee-001", "denied": False})

    def test_manual_notes_are_separate_from_cited_evidence_and_persist_in_docx(self):
        employee, admin = self.headers("employee"), self.headers("admin")
        created = self.client.post("/api/v1/artifacts", headers=employee, json={
            "kind": "docx", "title": "差旅整理", "goal": "核对审批", "source_ids": ["1"],
            "outline": ["审批流程"],
        }).json()
        artifact_id = created["id"]
        endpoint = f"/api/v1/artifacts/{artifact_id}/notes"
        notes = [{"heading": "审批流程", "text": "人工核对后补充：请联系财务。"}]
        self.assertEqual(self.client.post(endpoint, headers=admin, json={
            "draft_token": created["draft_token"], "notes": notes,
        }).status_code, 404)
        saved = self.client.post(endpoint, headers=employee, json={
            "draft_token": created["draft_token"], "notes": notes,
        })
        self.assertEqual(saved.status_code, 200)
        section = saved.json()["section_drafts"][0]
        self.assertEqual(section["manual_note"], notes[0]["text"])
        self.assertEqual(section["manual_note_by"], "employee-001")
        self.assertEqual(section["text"], created["section_drafts"][0]["text"])
        self.assertEqual(section["citation"], created["section_drafts"][0]["citation"])
        self.assertNotEqual(saved.json()["draft_token"], created["draft_token"])
        self.assertEqual(self.client.post(endpoint, headers=employee, json={
            "draft_token": created["draft_token"], "notes": notes,
        }).json()["code"], "DRAFT_CHANGED")
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                          json={"outline": ["审批流程"],
                                                "draft_token": created["draft_token"]}).json()["code"], "DRAFT_CHANGED")
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                          json={"outline": ["审批流程"],
                                                "draft_token": saved.json()["draft_token"]}).status_code, 200)
        payload = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).content
        paragraphs = [p.text for p in Document(io.BytesIO(payload)).paragraphs]
        self.assertIn("人工补充（employee-001）：人工核对后补充：请联系财务。", paragraphs)
        self.assertTrue(any(text.startswith("来源：") for text in paragraphs))

    def test_presentation_note_validation_reset_and_revocation(self):
        employee, admin = self.headers("employee"), self.headers("admin")
        created = self.client.post("/api/v1/artifacts", headers=employee, json={
            "kind": "pptx", "title": "差旅汇报", "goal": "例会", "source_ids": ["1"],
            "outline": ["审批流程"],
        }).json()
        artifact_id = created["id"]
        endpoint = f"/api/v1/artifacts/{artifact_id}/notes"
        self.assertEqual(self.client.post(endpoint, headers=employee, json={
            "draft_token": created["draft_token"],
            "notes": [{"heading": "审批流程", "text": "x" * 161}],
        }).json()["code"], "INVALID_NOTE")
        saved = self.client.post(endpoint, headers=employee, json={
            "draft_token": created["draft_token"],
            "notes": [{"heading": "审批流程", "text": "人工核对进度"}],
        }).json()
        self.assertEqual(saved["section_drafts"][0]["manual_note"], "人工核对进度")
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=employee,
                                          json={"outline": ["审批流程"]}).json()["section_drafts"][0].get("manual_note"), None)
        refreshed = self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()
        saved = self.client.post(endpoint, headers=employee, json={
            "draft_token": refreshed["draft_token"],
            "notes": [{"heading": "审批流程", "text": "人工核对进度"}],
        }).json()
        self.assertEqual(self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                                         json={"user_id": "employee-001", "denied": True}).status_code, 204)
        self.assertEqual(self.client.post(endpoint, headers=employee, json={
            "draft_token": saved["draft_token"],
            "notes": [{"heading": "审批流程", "text": "不能越权保存"}],
        }).status_code, 404)
        self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                        json={"user_id": "employee-001", "denied": False})
        self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                         json={"outline": ["审批流程"], "draft_token": saved["draft_token"]})
        deck = Presentation(io.BytesIO(self.client.get(f"/api/v1/artifacts/{artifact_id}/download",
                                       headers=employee).content))
        self.assertIn("人工补充（employee-001）：人工核对进度", deck.slides[0].placeholders[1].text)

    def test_presentation_theme_is_saved_and_rendered(self):
        employee = self.headers("employee")
        for theme, background in (("brief", (232, 242, 235)), ("strategy", (37, 62, 73))):
            with self.subTest(theme=theme):
                created = self.client.post("/api/v1/artifacts", headers=employee, json={
                    "kind": "pptx", "title": "差旅说明", "goal": "部门例会", "theme": theme,
                    "source_ids": ["1"], "outline": ["审批流程"],
                })
                self.assertEqual(created.status_code, 201)
                artifact_id = created.json()["id"]
                self.assertEqual(created.json()["theme"], theme)
                self.assertEqual(created.json()["brand_name"], "星图科技")
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()["theme"], theme)
                confirmed = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                             json={"outline": ["审批流程"]})
                self.assertEqual(confirmed.status_code, 200)
                payload = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).content
                slide = Presentation(io.BytesIO(payload)).slides[0]
                self.assertEqual(tuple(slide.background.fill.fore_color.rgb), background)
                self.assertEqual(slide.shapes.title.text, "审批流程")
                self.assertEqual(slide.placeholders[1].text, created.json()["section_drafts"][0]["text"])
                texts = [shape.text for shape in slide.shapes if shape.has_text_frame]
                self.assertTrue(any("差旅说明 · 部门例会" in text for text in texts))
                self.assertFalse(any("差旅报销管理制度 v2.1" in text for text in texts))
                self.assertIn("星图科技", texts)
                brand = next(shape for shape in slide.shapes if shape.has_text_frame and shape.text == "星图科技")
                self.assertLess(brand.top, slide.shapes.title.top)
                self.assertIn("01 / 01", texts)

        invalid = self.client.post("/api/v1/artifacts", headers=employee, json={
            "kind": "pptx", "title": "非法模板", "goal": "", "theme": "unknown",
            "source_ids": ["1"], "outline": ["审批流程"],
        })
        self.assertEqual(invalid.status_code, 422)

    def test_presentation_layouts_embed_images_and_keep_text_editable(self):
        for theme in ("brief", "strategy"):
            for count in (6, 8, 10):
                with self.subTest(theme=theme, count=count):
                    drafts = [{"heading": f"第 {index + 1} 页", "text": "要点说明 " * 12,
                               "evidence_gap": False,
                               "citation": {"title": "测试资料", "version": "v1", "location": "第 1 页"}}
                              for index in range(count)]
                    deck = Presentation(io.BytesIO(make_file("pptx", "季度进展", "部门例会", drafts,
                                                          theme, brand_name="星图科技")))
                    self.assertEqual(len(deck.slides), count)
                    pictures = [sum(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in slide.shapes)
                                for slide in deck.slides]
                    self.assertGreaterEqual(sum(pictures), 3)
                    self.assertGreater(pictures[0], 0)
                    self.assertGreater(pictures[-1], 0)
                    self.assertGreater(len({tuple(slide.background.fill.fore_color.rgb) for slide in deck.slides}), 1)
                    self.assertNotEqual(deck.slides[0].shapes.title.left, deck.slides[-1].shapes.title.left)
                    for index, slide in enumerate(deck.slides):
                        self.assertEqual(slide.shapes.title.text, drafts[index]["heading"])
                        brand = next(shape for shape in slide.shapes
                                     if shape.has_text_frame and shape.text == "星图科技")
                        self.assertLess(brand.left, deck.slide_width / 4)
                        self.assertLess(brand.top, slide.shapes.title.top)
                        on_photo = index == count - 1 or (
                            index > 0 and index % 2 == 1 and (index + 1) % 4 == 0)
                        self.assertEqual(brand.text_frame.paragraphs[0].font.color.rgb,
                                         RGBColor(246, 249, 245) if on_photo else
                                         slide.shapes.title.text_frame.paragraphs[0].font.color.rgb)
                        self.assertEqual(slide.placeholders[1].text, drafts[index]["text"].strip())
                        self.assertFalse(any("测试资料 v1" in shape.text for shape in slide.shapes
                                             if shape.has_text_frame))

    def test_markdown_sections_keep_evidence_with_their_heading(self):
        from app.api.content import split_segments

        segments = split_segments("# 项目介绍\n\n定位为工作流调度。\n\n## MasterServer\n\n负责 DAG 任务切分。\n\n## WorkerServer\n\n负责实际任务执行。")
        self.assertEqual(len(segments), 3)
        self.assertIn("MasterServer 负责 DAG 任务切分", segments[1])
        self.assertNotIn("WorkerServer", segments[1])
        self.assertIn("WorkerServer 负责实际任务执行", segments[2])

    def test_long_presentation_titles_do_not_intrude_into_the_body(self):
        draft = {"heading": "核心功能：任务类型与工作流操作", "text": "支持多种任务类型。",
                 "evidence_gap": False, "citation": None}
        slide = Presentation(io.BytesIO(make_file(
            "pptx", "Apache DolphinScheduler 公开项目技术汇报",
            "基于官方文档摘录的功能与架构介绍", [draft]))).slides[0]
        self.assertGreater(slide.placeholders[1].top, slide.shapes.title.top + slide.shapes.title.height)
        self.assertLessEqual(slide.shapes.title.text_frame.paragraphs[0].font.size.pt, 34)
        kickers = [shape.text for shape in slide.shapes if shape.has_text_frame
                   and shape.text.startswith("Apache DolphinScheduler")]
        self.assertTrue(kickers)
        self.assertTrue(kickers[0].endswith("..."))

    def test_comparison_draft_preserves_each_source_and_missing_evidence(self):
        employee = self.headers("employee")
        admin = self.headers("admin")
        body = {"kind": "docx", "mode": "comparison", "title": "制度对比", "goal": "核对审批流程",
                "source_ids": ["1", "2"], "outline": ["审批流程"]}
        self.assertEqual(self.client.post("/api/v1/artifacts", headers=employee,
                                          json=body | {"source_ids": ["1"]}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/artifacts", headers=employee,
                                          json=body | {"kind": "pptx"}).status_code, 422)
        created = self.client.post("/api/v1/artifacts", headers=employee, json=body)
        self.assertEqual(created.status_code, 201)
        artifact_id = created.json()["id"]
        section = created.json()["section_drafts"][0]
        self.assertEqual(created.json()["mode"], "comparison")
        self.assertEqual([source["document_id"] for source in section["sources"]], ["1", "2"])
        self.assertFalse(section["sources"][0]["evidence_gap"])
        self.assertTrue(section["sources"][1]["evidence_gap"])
        self.assertEqual(section["comparison_status"], "missing_evidence")
        self.assertIn("审批", section["sources"][0]["text"])
        self.assertEqual(section["sources"][1]["text"], "")

        shared = self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=employee,
                                  json={"outline": ["员工"]})
        self.assertEqual([source["evidence_gap"] for source in shared.json()["section_drafts"][0]["sources"]],
                         [False, False])
        self.assertEqual(shared.json()["section_drafts"][0]["comparison_status"], "review_required")
        empty_difference = self.client.post(f"/api/v1/artifacts/{artifact_id}/review", headers=employee,
                                            json={"reviews": [{"heading": "员工", "decision": "different", "note": ""}]})
        self.assertEqual((empty_difference.status_code, empty_difference.json()["code"]), (422, "INVALID_REVIEW"))
        difference = self.client.post(f"/api/v1/artifacts/{artifact_id}/review", headers=employee,
                                      json={"reviews": [{"heading": "员工", "decision": "different", "note": "适用条件不同"}]})
        self.assertEqual(difference.json()["section_drafts"][0]["review"]["decision"], "different")

        preview = self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=employee,
                                   json={"outline": ["年假标准"]})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["section_drafts"][0]["sources"][0]["evidence_gap"], True)
        self.assertEqual(preview.json()["section_drafts"][0]["sources"][1]["evidence_gap"], False)
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()["section_drafts"],
                         preview.json()["section_drafts"])
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                          json={"outline": ["未经预览的章节"]}).status_code, 409)
        missing_review = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                          json={"outline": ["年假标准"]})
        self.assertEqual((missing_review.status_code, missing_review.json()["code"]), (409, "REVIEW_REQUIRED"))
        review_url = f"/api/v1/artifacts/{artifact_id}/review"
        self.assertEqual(self.client.post(review_url, headers=admin, json={"reviews": [
            {"heading": "年假标准", "decision": "unresolved", "note": "待补充"}
        ]}).status_code, 404)
        self.assertEqual(self.client.post(review_url, headers=employee, json={"reviews": [
            {"heading": "旧标题", "decision": "unresolved", "note": "待补充"}
        ]}).status_code, 409)
        invalid = self.client.post(review_url, headers=employee, json={"reviews": [
            {"heading": "年假标准", "decision": "aligned", "note": ""}
        ]})
        self.assertEqual((invalid.status_code, invalid.json()["code"]), (422, "INVALID_REVIEW"))
        review_body = {"reviews": [{"heading": "年假标准", "decision": "unresolved", "note": "缺少差旅制度证据"}]}
        reviewed = self.client.post(review_url, headers=employee, json=review_body)
        self.assertEqual(reviewed.status_code, 200)
        saved_review = reviewed.json()["section_drafts"][0]["review"]
        self.assertEqual(saved_review["reviewed_by"], "employee-001")
        self.assertEqual(saved_review["note"], "缺少差旅制度证据")
        self.assertTrue(saved_review["reviewed_at"])
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()["section_drafts"],
                         reviewed.json()["section_drafts"])
        refreshed = self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=employee,
                                     json={"outline": ["年假标准"]})
        self.assertNotIn("review", refreshed.json()["section_drafts"][0])
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                          json={"outline": ["年假标准"]}).json()["code"], "REVIEW_REQUIRED")
        reviewed = self.client.post(review_url, headers=employee, json=review_body)
        confirmed = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                     json={"outline": ["年假标准"]})
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["section_drafts"], reviewed.json()["section_drafts"])
        payload = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).content
        paragraphs = [paragraph.text for paragraph in Document(io.BytesIO(payload)).paragraphs]
        self.assertIn(preview.json()["section_drafts"][0]["sources"][1]["text"], paragraphs)
        self.assertIn("待补充证据：此来源未找到与本主题直接相关的片段。", paragraphs)
        self.assertIn("人工核对结论：证据不足，待核对", paragraphs)
        self.assertIn("人工备注：缺少差旅制度证据", paragraphs)
        self.assertFalse(any("冲突已确认" in text for text in paragraphs))
        self.client.put("/api/v1/admin/documents/2/deny", headers=admin,
                        json={"user_id": "employee-001", "denied": True})
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).status_code, 404)

    def test_section_evidence_preview_matches_export_and_rechecks_access(self):
        employee = self.headers("employee")
        admin = self.headers("admin")
        for kind in ("docx", "pptx"):
            with self.subTest(kind=kind):
                created = self.client.post("/api/v1/artifacts", headers=employee, json={
                    "kind": kind, "title": "制度整理", "goal": "核对两项制度", "source_ids": ["1", "2"],
                    "outline": ["审批流程", "年假标准", "营销预算"],
                })
                self.assertEqual(created.status_code, 201)
                artifact_id = created.json()["id"]
                drafts = created.json()["section_drafts"]
                self.assertEqual([draft["evidence_gap"] for draft in drafts], [False, False, True])
                self.assertEqual([draft["citation"]["document_id"] for draft in drafts[:2]], ["1", "2"])
                self.assertIn("审批", drafts[0]["text"])
                self.assertNotIn("年假", drafts[0]["text"])
                self.assertEqual(drafts[2]["text"], "")
                self.assertIsNone(drafts[2]["citation"])
                rejected = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                            json={"outline": ["未经预览的新标题"]})
                self.assertEqual((rejected.status_code, rejected.json()["code"]), (409, "DRAFT_CHANGED"))
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()["status"], "pending")
                by_source = self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=employee,
                                             json={"outline": ["差旅报销管理制度", "员工年假与休假管理办法"]})
                self.assertEqual([section["citation"]["document_id"] for section in by_source.json()["section_drafts"]], ["1", "2"])

                changed = self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=employee,
                                           json={"outline": ["财务审核", "年假标准", "营销预算"]})
                self.assertEqual(changed.status_code, 200)
                self.assertEqual(changed.json()["section_drafts"][0]["heading"], "财务审核")
                resumed = self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()
                self.assertEqual(resumed["outline"][0], "财务审核")
                self.assertEqual(resumed["section_drafts"], changed.json()["section_drafts"])
                self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/preview", headers=admin,
                                                  json={"outline": ["审批流程"]}).status_code, 404)

                confirmed = self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                             json={"outline": changed.json()["outline"]})
                self.assertEqual(confirmed.status_code, 200)
                drafts = confirmed.json()["section_drafts"]
                self.assertEqual(drafts, changed.json()["section_drafts"])
                payload = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).content
                if kind == "docx":
                    paragraphs = [p.text for p in Document(io.BytesIO(payload)).paragraphs]
                    self.assertIn(drafts[0]["text"], paragraphs)
                    self.assertIn(drafts[1]["text"], paragraphs)
                    self.assertIn("待补充证据：所选资料未找到与本章节直接相关的片段。", paragraphs)
                    self.assertIn("来源：差旅报销管理制度 v2.1 · 第 3 页 · 审批流程 · 片段 1", paragraphs)
                else:
                    slides = Presentation(io.BytesIO(payload)).slides
                    self.assertEqual(slides[0].placeholders[1].text, drafts[0]["text"])
                    self.assertEqual(slides[1].placeholders[1].text, drafts[1]["text"])
                    self.assertEqual(slides[2].placeholders[1].text, "待补充证据：本页没有匹配片段。")
                self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                                json={"user_id": "employee-001", "denied": True})
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).status_code, 404)
                self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).status_code, 404)
                self.client.put("/api/v1/admin/documents/1/deny", headers=admin,
                                json={"user_id": "employee-001", "denied": False})

    def test_artifact_source_version_is_pinned_at_creation_and_download(self):
        employee = self.headers("employee")
        def create():
            response = self.client.post("/api/v1/artifacts", headers=employee, json={
                "kind": "docx", "title": "差旅整理", "goal": "核对流程", "source_ids": ["1"],
                "outline": ["审批流程"],
            })
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["source_versions"], {"1": "v2.1"})
            return response.json()["id"]

        pending_id = create()
        self.documents._db.execute("UPDATE documents SET version = 'v2.2' WHERE document_id = '1'")
        self.documents._db.commit()
        pending = self.client.get(f"/api/v1/artifacts/{pending_id}", headers=employee).json()
        self.assertEqual(pending["source_status"], "stale")
        self.assertEqual(pending["section_drafts"], [])
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{pending_id}/preview", headers=employee,
                                          json={"outline": ["审批流程"]}).status_code, 409)
        confirm = self.client.post(f"/api/v1/artifacts/{pending_id}/confirm", headers=employee,
                                   json={"outline": ["审批流程"]})
        self.assertEqual((confirm.status_code, confirm.json()["code"]), (409, "SOURCE_VERSION_CHANGED"))

        fresh = self.client.post("/api/v1/artifacts", headers=employee, json={
            "kind": "docx", "title": "差旅整理", "goal": "核对流程", "source_ids": ["1"],
            "outline": ["审批流程"],
        }).json()
        self.assertEqual(fresh["source_versions"], {"1": "v2.2"})
        artifact_id = fresh["id"]
        self.assertEqual(self.client.post(f"/api/v1/artifacts/{artifact_id}/confirm", headers=employee,
                                          json={"outline": ["审批流程"]}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee).status_code, 200)
        self.documents._db.execute("UPDATE documents SET version = 'v2.3' WHERE document_id = '1'")
        self.documents._db.commit()
        download = self.client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=employee)
        self.assertEqual((download.status_code, download.json()["code"]), (409, "SOURCE_VERSION_CHANGED"))
        stale = self.client.get(f"/api/v1/artifacts/{artifact_id}", headers=employee).json()
        self.assertEqual(stale["source_status"], "stale")
        self.assertEqual(stale["section_drafts"], [])

    def test_legacy_artifact_migration_keeps_record_without_claiming_version_check(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifacts.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE artifacts (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                kind TEXT NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL,
                source_ids TEXT NOT NULL, outline TEXT NOT NULL, status TEXT NOT NULL, content BLOB
            )""")
            connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               ("legacy", "demo-org", "employee-001", "docx", "旧任务", "", '["1"]',
                                '["审批流程"]', "completed", b"legacy-file"))
            connection.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               ("legacy-pending", "demo-org", "employee-001", "docx", "旧草稿", "", '["1"]',
                                '["审批流程"]', "pending", None))
            connection.commit()
            connection.close()
            repository = ArtifactRepository(path)
            subject = Subject("employee-001", "demo-org", frozenset({"general"}), Classification.INTERNAL)
            item = repository.get(subject, "legacy", self.documents)
            self.assertEqual(item["source_status"], "unverified")
            self.assertEqual(item["theme"], "brief")
            self.assertIsNone(item["source_versions"])
            self.assertEqual(repository.download(subject, "legacy", self.documents), ("docx", b"legacy-file"))
            confirmed = repository.confirm(subject, "legacy-pending", self.documents, ["审批流程"])
            self.assertEqual(confirmed["source_status"], "current")
            self.assertEqual(confirmed["source_versions"], {"1": "v2.1"})
            repository._db.close()

    def test_upload_rejects_bad_or_empty_file(self):
        manager = self.headers("manager")
        for name, content in (("bad.exe", b"abc"), ("empty.txt", b"")):
            response = self.client.post("/api/v1/documents/upload", headers=manager,
                data={"title": "无效", "area": "产品资料", "visibility": "部门受限"},
                files={"file": (name, content, "application/octet-stream")})
            self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
