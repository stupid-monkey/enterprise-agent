"""Editable local DOCX/PPTX artifacts with source reauthorization."""

import io
import json
import hashlib
import re
import httpx
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from docx import Document
from docx.shared import Inches as DocumentInches, Pt
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR
from pptx.enum.text import PP_ALIGN
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt as SlidePt

from app.api.content import search_terms, split_segments
from app.api.documents import DocumentRepository, OriginalVersionChanged
from app.api.collaboration import CollaborationModel, run_collaboration
from app.api.slide_visuals import (UnsplashPhotoProvider, best_document_visual,
                                   embedded_visuals, local_asset_for, visual_basis, visual_theme)
from app.agent.reflection import ReflectionInvalid, ReflectionLimits, ReflectionModel, run_reflection
from app.security.policy import ResourceAction, Subject


class SourceVersionChanged(Exception):
    pass


class DraftChanged(Exception):
    pass


class ReviewRequired(Exception):
    pass


SLIDE_ASSETS = Path(__file__).resolve().parents[1] / "slide_assets"


def organization_brand(org_id: str) -> str:
    return "星图科技" if org_id == "demo-org" else org_id


def _slide_layout(index: int, count: int) -> str:
    if index == 0:
        return "cover"
    if index == count - 1:
        return "closing"
    return "split" if index % 2 else "focus"


def _slide_photo(slide, name: str | bytes, left: float, top: float, width: float, height: float,
                 contain: bool = False) -> None:
    # Crop within the PPTX picture shape so the embedded image remains replaceable.
    source = io.BytesIO(name) if isinstance(name, bytes) else str(SLIDE_ASSETS / name)
    from PIL import Image
    with Image.open(io.BytesIO(name) if isinstance(name, bytes) else SLIDE_ASSETS / name) as image:
        source_ratio = image.width / image.height
    box_ratio = width / height
    if contain:
        _slide_rect(slide, left, top, width, height, RGBColor(246, 249, 245))
        if source_ratio > box_ratio:
            fitted_width, fitted_height = width, width / source_ratio
        else:
            fitted_width, fitted_height = height * source_ratio, height
        slide.shapes.add_picture(source, Inches(left + (width - fitted_width) / 2),
                                 Inches(top + (height - fitted_height) / 2),
                                 Inches(fitted_width), Inches(fitted_height))
        return
    picture = slide.shapes.add_picture(source, Inches(left), Inches(top), Inches(width), Inches(height))
    if source_ratio > box_ratio:
        crop = (1 - box_ratio / source_ratio) / 2
        picture.crop_left = picture.crop_right = crop
    else:
        crop = (1 - source_ratio / box_ratio) / 2
        picture.crop_top = picture.crop_bottom = crop


def _slide_rect(slide, left: float, top: float, width: float, height: float, color: RGBColor) -> None:
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()


def _slide_label(slide, text: str, left: float, top: float, width: float, height: float,
                 size: int, color: RGBColor, bold: bool = False) -> None:
    shape = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = shape.text_frame
    frame.clear()
    frame.margin_left = frame.margin_right = Inches(0)
    frame.margin_top = frame.margin_bottom = Inches(0)
    paragraph = frame.paragraphs[0]
    paragraph.text = text
    paragraph.font.name = "Microsoft YaHei"
    paragraph.font.size = SlidePt(size)
    paragraph.font.bold = bold
    paragraph.font.color.rgb = color


def _slide_text_units(text: str) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in text)


def _slide_fit_label(text: str, units: int) -> str:
    fitted = ""
    for char in text:
        if _slide_text_units(fitted + char) > units:
            return fitted.rstrip() + "..."
        fitted += char
    return fitted


class InvalidReview(Exception):
    pass


class InvalidNote(Exception):
    pass


class InvalidEdit(Exception):
    pass


class EditSourceTooLarge(Exception):
    pass


class CompositionConflict(Exception):
    pass


class CompositionNoEvidence(Exception):
    pass


class ArtifactRepository:
    def __init__(self, path: str | Path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                kind TEXT NOT NULL, title TEXT NOT NULL, goal TEXT NOT NULL,
                source_ids TEXT NOT NULL, outline TEXT NOT NULL,
                status TEXT NOT NULL, content BLOB, source_versions TEXT,
                section_drafts TEXT, theme TEXT NOT NULL DEFAULT 'brief',
                mode TEXT NOT NULL DEFAULT 'standard'
            )""")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(artifacts)")}
            if "source_versions" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN source_versions TEXT")
            if "section_drafts" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN section_drafts TEXT")
            if "theme" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN theme TEXT NOT NULL DEFAULT 'brief'")
            if "mode" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'")
            if "collaboration_trace" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN collaboration_trace TEXT")
            if "collaboration_request_id" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN collaboration_request_id TEXT")
            if "composition_request_id" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN composition_request_id TEXT")
            if "composition_input_hash" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN composition_input_hash TEXT")
            if "composition_plan" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN composition_plan TEXT")
            if "reflection_trace" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN reflection_trace TEXT")
            if "reflection_request_id" not in columns:
                self._db.execute("ALTER TABLE artifacts ADD COLUMN reflection_request_id TEXT")
            self._db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS artifacts_composition_request
                ON artifacts(org_id, owner_id, composition_request_id)
                WHERE composition_request_id IS NOT NULL""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS artifact_edit_proposals (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL, draft_token TEXT NOT NULL, section_index INTEGER NOT NULL,
                before_text TEXT NOT NULL, revised_text TEXT NOT NULL, source_quote TEXT NOT NULL,
                instruction TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
            )""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS artifact_slide_visuals (
                artifact_id TEXT NOT NULL, page_index INTEGER NOT NULL, basis TEXT NOT NULL,
                category TEXT NOT NULL, source TEXT NOT NULL, asset_name TEXT,
                photo_id TEXT, photographer TEXT, page_url TEXT, license_url TEXT,
                image BLOB, source_document_id TEXT, source_version TEXT,
                source_title TEXT, source_location TEXT,
                PRIMARY KEY (artifact_id, page_index)
            )""")
            visual_columns = {row["name"] for row in self._db.execute("PRAGMA table_info(artifact_slide_visuals)")}
            for column in ("source_document_id", "source_version", "source_title", "source_location"):
                if column not in visual_columns:
                    self._db.execute(f"ALTER TABLE artifact_slide_visuals ADD COLUMN {column} TEXT")

    @staticmethod
    def _visible_sources(subject: Subject, documents: DocumentRepository, ids: list[str]):
        if not ids or len(ids) > 10 or len(set(ids)) != len(ids):
            return None
        sources = [documents.text_for(subject, document_id, ResourceAction.USE_FOR_GENERATION) for document_id in ids]
        return None if any(source is None for source in sources) else sources

    @classmethod
    def suggest_slide_outline(cls, subject: Subject, documents: DocumentRepository,
                              source_ids: list[str], count: int) -> list[str] | None:
        sources = cls._visible_sources(subject, documents, source_ids)
        if sources is None:
            return None
        candidates: list[str] = []
        for _, content in sources:
            section_headings: list[str] = []
            feature_headings: list[str] = []
            for line in content.splitlines():
                line = line.strip()
                section = re.match(r"^#{2,4}\s+(.+)$", line)
                feature = re.match(r"^[-*]\s+\*\*(.+?)\*\*", line)
                match = feature or section
                if not match:
                    continue
                heading = re.sub(r"\s+", " ", match.group(1)).strip(" :：#*")[:100]
                target = feature_headings if feature else section_headings
                if heading and heading not in target:
                    target.append(heading)
            source_headings = feature_headings or section_headings
            if not source_headings:
                source_headings = [re.sub(r"^[#*\-\s]+", "", segment).splitlines()[0][:60].strip()
                                   for segment in split_segments(content)]
            for heading in source_headings:
                if heading and heading not in candidates:
                    candidates.append(heading)
        supported = cls._drafts(candidates, sources)
        return [draft["heading"] for draft in supported if not draft["evidence_gap"]][:count]

    @staticmethod
    def _drafts(outline: list[str], sources: list[tuple]) -> list[dict]:
        drafts = []
        for heading in outline:
            terms = search_terms(heading, question=True)
            candidates = []
            for record, text in sources:
                for number, segment in enumerate(split_segments(text), start=1):
                    score = sum(term in segment.lower() for term in terms)
                    if heading in record.title or record.title in heading:
                        score += 2 * len(terms)
                    if score > 0:
                        location = (f"{record.location} · 片段 {number}" if record.location != "正文"
                                    else f"正文 · 片段 {number}")
                        candidates.append((-score, record.document_id, number, segment, record, location))
            candidates.sort(key=lambda item: (item[0], item[1], item[2]))
            if candidates:
                _, _, _, excerpt, record, location = candidates[0]
                draft = {"heading": heading, "text": excerpt[:450], "evidence_gap": False,
                         "citation": {"document_id": record.document_id, "title": record.title,
                                      "version": record.version, "location": location}}
                if len(sources) > 1:
                    seen = set()
                    supporting = []
                    for _, document_id, _, candidate, candidate_record, candidate_location in candidates:
                        if document_id in seen:
                            continue
                        seen.add(document_id)
                        supporting.append({"text": candidate[:450],
                                           "citation": {"document_id": document_id,
                                                        "title": candidate_record.title,
                                                        "version": candidate_record.version,
                                                        "location": candidate_location}})
                        if len(supporting) == 2:
                            break
                    draft["supporting_sources"] = supporting
                drafts.append(draft)
            else:
                draft = {"heading": heading, "text": "", "evidence_gap": True, "citation": None}
                if len(sources) > 1:
                    draft["supporting_sources"] = []
                drafts.append(draft)
        return drafts

    @staticmethod
    def _comparison_drafts(outline: list[str], sources: list[tuple]) -> list[dict]:
        drafts = []
        for heading in outline:
            terms = search_terms(heading, question=True)
            rows = []
            for record, text in sources:
                candidates = []
                for number, segment in enumerate(split_segments(text), start=1):
                    score = sum(term in segment.lower() for term in terms)
                    if score > 0:
                        candidates.append((-score, number, segment))
                candidates.sort(key=lambda item: (item[0], item[1]))
                citation = None
                excerpt = ""
                if candidates:
                    _, number, segment = candidates[0]
                    excerpt = segment[:450]
                    location = (f"{record.location} · 片段 {number}" if record.location != "正文"
                                else f"正文 · 片段 {number}")
                    citation = {"document_id": record.document_id, "title": record.title,
                                "version": record.version, "location": location}
                rows.append({"document_id": record.document_id, "title": record.title,
                             "version": record.version, "text": excerpt, "evidence_gap": citation is None,
                             "citation": citation})
            drafts.append({"heading": heading, "text": "", "evidence_gap": any(row["evidence_gap"] for row in rows),
                           "citation": None, "sources": rows,
                           "comparison_status": "missing_evidence" if any(row["evidence_gap"] for row in rows)
                           else "review_required"})
        return drafts

    @classmethod
    def _make_drafts(cls, outline: list[str], sources: list[tuple], mode: str) -> list[dict]:
        return cls._comparison_drafts(outline, sources) if mode == "comparison" else cls._drafts(outline, sources)

    @staticmethod
    def _draft_token(drafts: list[dict]) -> str:
        return hashlib.sha256(json.dumps(drafts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def create(self, subject: Subject, documents: DocumentRepository, *, kind: str, title: str,
               goal: str, source_ids: list[str], outline: list[str], theme: str = "brief",
               mode: str = "standard", composition_request_id: str | None = None,
               composition_input_hash: str | None = None,
               composition_plan: dict | None = None,
               expected_versions: dict[str, str] | None = None) -> dict | None:
        sources = self._visible_sources(subject, documents, source_ids)
        if sources is None:
            return None
        artifact_id = uuid4().hex
        versions = {record.document_id: record.version for record, _ in sources}
        if expected_versions is not None and versions != expected_versions:
            raise SourceVersionChanged()
        drafts = self._make_drafts(outline, sources, mode)
        if composition_plan is not None and all(draft["evidence_gap"] for draft in drafts):
            raise CompositionNoEvidence()
        with self._lock, self._db:
            self._db.execute("""INSERT INTO artifacts
                (id, org_id, owner_id, kind, title, goal, source_ids, outline, status, content,
                 source_versions, section_drafts, theme, mode, composition_request_id,
                 composition_input_hash, composition_plan)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (artifact_id, subject.org_id, subject.user_id, kind, title, goal,
                              json.dumps(source_ids), json.dumps(outline), "pending", None, json.dumps(versions),
                              json.dumps(drafts, ensure_ascii=False), theme, mode,
                              composition_request_id, composition_input_hash,
                              json.dumps(composition_plan, ensure_ascii=False) if composition_plan else None))
        return self.get(subject, artifact_id, documents)

    def create_editable_copy(self, subject: Subject, document_id: str,
                             documents: DocumentRepository) -> dict | None:
        source = documents.text_for(subject, document_id, ResourceAction.USE_FOR_GENERATION)
        if source is None:
            return None
        record, content = source
        segments = split_segments(content, max_chars=450)
        if not segments or len(segments) > 10:
            raise EditSourceTooLarge()
        kind = "pptx" if record.file_type.upper() == "PPTX" else "docx"
        outline = [f"内容片段 {index}" for index in range(1, len(segments) + 1)]
        drafts = [{"heading": heading, "text": segment, "evidence_gap": False,
                   "citation": {"document_id": record.document_id, "title": record.title,
                                "version": record.version, "location": f"{record.location} · 片段 {index}"}}
                  for index, (heading, segment) in enumerate(zip(outline, segments), start=1)]
        with self._lock, self._db:
            latest = documents.text_for(subject, document_id, ResourceAction.USE_FOR_GENERATION)
            if latest is None:
                return None
            if latest[0].version != record.version:
                raise SourceVersionChanged()
            artifact_id = uuid4().hex
            self._db.execute("""INSERT INTO artifacts
                (id, org_id, owner_id, kind, title, goal, source_ids, outline, status, content,
                 source_versions, section_drafts, theme, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, 'brief', 'standard')""",
                (artifact_id, subject.org_id, subject.user_id, kind, f"{record.title[:110]} - 编辑副本",
                 "基于授权资料生成的个人编辑副本", json.dumps([document_id]),
                 json.dumps(outline, ensure_ascii=False), json.dumps({document_id: record.version}),
                 json.dumps(drafts, ensure_ascii=False)))
        return self.get(subject, artifact_id, documents)

    def discard_editable_copy(self, subject: Subject, artifact_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("""DELETE FROM artifact_edit_proposals
                WHERE artifact_id = ? AND org_id = ? AND owner_id = ? AND status = 'pending'""",
                (artifact_id, subject.org_id, subject.user_id))
            self._db.execute("""DELETE FROM artifacts
                WHERE id = ? AND org_id = ? AND owner_id = ? AND status = 'pending'""",
                (artifact_id, subject.org_id, subject.user_id))

    def composed_for(self, subject: Subject, documents: DocumentRepository, *,
                     question: str, request_id: str,
                     input_hash: str | None = None) -> dict | None:
        input_hash = input_hash or hashlib.sha256(question.strip().encode("utf-8")).hexdigest()
        with self._lock:
            row = self._db.execute("""SELECT id, composition_input_hash FROM artifacts
                WHERE org_id = ? AND owner_id = ? AND composition_request_id = ?""",
                (subject.org_id, subject.user_id, request_id)).fetchone()
            if row is None:
                return None
            if row["composition_input_hash"] != input_hash:
                raise CompositionConflict()
            item = self.get(subject, row["id"], documents)
            if item is None:
                raise CompositionNoEvidence()
            if item["source_status"] != "current":
                raise SourceVersionChanged()
            return item | {"composition_created": False}

    def create_planned_from_question(self, subject: Subject, documents: DocumentRepository, *,
                                     question: str, request_id: str, title: str,
                                     source_ids: list[str], outline: list[str], plan: dict,
                                     source_versions: dict[str, str],
                                     input_hash: str | None = None) -> dict:
        with self._lock:
            existing = self.composed_for(subject, documents, question=question,
                                         request_id=request_id, input_hash=input_hash)
            if existing is not None:
                return existing
            item = self.create(
                subject, documents, kind="docx", title=title, goal=question.strip(),
                source_ids=source_ids, outline=outline, composition_request_id=request_id,
                composition_input_hash=input_hash or hashlib.sha256(question.strip().encode("utf-8")).hexdigest(),
                composition_plan=plan, expected_versions=source_versions)
            if item is None:
                raise CompositionNoEvidence()
            return item | {"composition_created": True}

    def create_from_question(self, subject: Subject, documents: DocumentRepository, *,
                             question: str, request_id: str) -> dict:
        question = question.strip()
        input_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._db.execute("""SELECT id, composition_input_hash FROM artifacts
                WHERE org_id = ? AND owner_id = ? AND composition_request_id = ?""",
                (subject.org_id, subject.user_id, request_id)).fetchone()
            if row is not None:
                if row["composition_input_hash"] != input_hash:
                    raise CompositionConflict()
                item = self.get(subject, row["id"], documents)
                if item is None:
                    raise CompositionNoEvidence()
                if item["source_status"] != "current":
                    raise SourceVersionChanged()
                return item | {"composition_created": False}

            source_ids: list[str] = []
            for hit in documents.search_visible(subject, question, limit=12):
                document_id = hit.document.document_id
                if document_id not in source_ids and documents.text_for(
                    subject, document_id, ResourceAction.USE_FOR_GENERATION
                ) is not None:
                    source_ids.append(document_id)
                if len(source_ids) == 3:
                    break
            if not source_ids:
                raise CompositionNoEvidence()
            item = self.create(subject, documents, kind="docx", title=f"{question[:80]} · 资料整理",
                               goal=question, source_ids=source_ids, outline=[question[:100]],
                               composition_request_id=request_id, composition_input_hash=input_hash)
            if item is None:
                raise CompositionNoEvidence()
            return item | {"composition_created": True}

    def get(self, subject: Subject, artifact_id: str, documents: DocumentRepository) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM artifacts WHERE id = ? AND org_id = ? AND owner_id = ?",
                                   (artifact_id, subject.org_id, subject.user_id)).fetchone()
        if row is None:
            return None
        sources = self._visible_sources(subject, documents, json.loads(row["source_ids"]))
        if sources is None:
            return None
        versions = json.loads(row["source_versions"]) if row["source_versions"] is not None else None
        source_status = ("unverified" if versions is None else
                         "current" if versions == {record.document_id: record.version for record, _ in sources}
                         else "stale")
        if source_status == "stale":
            drafts = []
        elif row["section_drafts"] is not None:
            drafts = json.loads(row["section_drafts"])
        elif row["status"] == "pending":
            drafts = self._make_drafts(json.loads(row["outline"]), sources, row["mode"])
        else:
            drafts = []
        visuals = self._slide_visuals(row["id"], drafts) if row["kind"] == "pptx" and drafts else []
        return {key: row[key] for key in ("id", "kind", "title", "goal", "status", "theme", "mode")} | {
            "brand_name": organization_brand(row["org_id"]),
            "source_ids": json.loads(row["source_ids"]), "outline": json.loads(row["outline"]),
            "source_versions": versions, "source_status": source_status,
            "section_drafts": drafts, "draft_token": self._draft_token(drafts),
            "collaboration_trace": json.loads(row["collaboration_trace"]) if row["collaboration_trace"] and source_status == "current" else [],
            "reflection_trace": json.loads(row["reflection_trace"]) if row["reflection_trace"] and source_status == "current" else [],
            "composition_plan": json.loads(row["composition_plan"]) if row["composition_plan"] else None,
            "slide_visuals": visuals,
        }

    def _slide_visuals(self, artifact_id: str, drafts: list[dict]) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM artifact_slide_visuals WHERE artifact_id = ?",
                                    (artifact_id,)).fetchall()
        return [{key: row[key] for key in ("page_index", "category", "source", "asset_name",
                                          "photographer", "page_url", "license_url",
                                          "source_document_id", "source_version", "source_title",
                                          "source_location")}
                for row in rows if row["page_index"] < len(drafts)
                and row["basis"] == visual_basis(drafts[row["page_index"]])]

    def generate_slide_visuals(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                               draft_token: str, provider: UnsplashPhotoProvider | None) -> dict | None:
        item = self.get(subject, artifact_id, documents)
        if item is None:
            return None
        if item["source_status"] != "current":
            raise SourceVersionChanged()
        if item["kind"] != "pptx" or item["status"] != "pending" or item["draft_token"] != draft_token:
            raise DraftChanged()
        used_ids: set[str] = set()
        used_local_assets: set[str] = set()
        used_document_images: set[str] = set()
        image_candidates: dict[str, list[dict]] = {}
        online_failed = False
        remote_attempts = 0
        started = time.monotonic()
        visuals = []
        for index, draft in enumerate(item["section_drafts"]):
            if index not in (0, len(item["section_drafts"]) - 1) and index % 2 == 0:
                continue
            theme = visual_theme(item["title"], draft)
            result = None
            citations = [entry.get("citation") for entry in draft.get("ai_evidence", [])]
            citations.extend(source.get("citation") for source in draft.get("supporting_sources", []))
            citations.append(draft.get("citation"))
            cited_ids = {citation["document_id"] for citation in citations if citation and
                         citation.get("document_id") in item["source_ids"]}
            source_ids = [document_id for document_id in item["source_ids"] if document_id in cited_ids]
            for document_id in source_ids:
                if document_id not in image_candidates:
                    try:
                        original = documents.generation_original_for(
                            subject, document_id, item["source_versions"][document_id])
                    except OriginalVersionChanged as exc:
                        raise SourceVersionChanged() from exc
                    if original is None:
                        image_candidates[document_id] = []
                        continue
                    file_type, content = original
                    record = documents.text_for(subject, document_id, ResourceAction.USE_FOR_GENERATION)
                    if record is None or record[0].version != item["source_versions"][document_id]:
                        raise SourceVersionChanged()
                    image_candidates[document_id] = [candidate | {
                        "document_id": document_id, "version": record[0].version,
                        "title": record[0].title} for candidate in embedded_visuals(file_type, content)]
            candidates = [candidate for document_id in source_ids for candidate in image_candidates[document_id]]
            document_image = best_document_visual(draft["heading"], candidates, used_document_images)
            if document_image:
                used_document_images.add(document_image["image_id"])
            if (not document_image and provider is not None and not online_failed
                    and remote_attempts < 4 and time.monotonic() - started < 25):
                remote_attempts += 1
                try:
                    result = provider.find(theme.query, used_ids)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    online_failed = True
            if result:
                used_ids.add(result["photo_id"])
            asset_name = local_asset_for(theme, used_local_assets)
            if not result and not document_image:
                used_local_assets.add(asset_name)
            visuals.append((artifact_id, index, visual_basis(draft), theme.name,
                            "document" if document_image else "online" if result else "local", asset_name,
                            result["photo_id"] if result else None,
                            result["photographer"] if result else None,
                            result["page_url"] if result else None,
                            result["license_url"] if result else None,
                            document_image["image"] if document_image else result["image"] if result else None,
                            document_image["document_id"] if document_image else None,
                            document_image["version"] if document_image else None,
                            document_image["title"] if document_image else None,
                            document_image["location"] if document_image else None))
        with self._lock, self._db:
            current = self.get(subject, artifact_id, documents)
            if current is None:
                return None
            if (current["source_status"] != "current" or current["status"] != "pending"
                    or current["draft_token"] != draft_token):
                raise DraftChanged()
            self._db.execute("DELETE FROM artifact_slide_visuals WHERE artifact_id = ?", (artifact_id,))
            self._db.executemany("""INSERT INTO artifact_slide_visuals
                (artifact_id, page_index, basis, category, source, asset_name, photo_id,
                 photographer, page_url, license_url, image, source_document_id,
                 source_version, source_title, source_location)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", visuals)
            return self.get(subject, artifact_id, documents)

    def slide_visual_image(self, subject: Subject, artifact_id: str, page_index: int,
                           documents: DocumentRepository) -> bytes | None:
        item = self.get(subject, artifact_id, documents)
        if item is None or item["source_status"] != "current" or item["kind"] != "pptx":
            return None
        if not any(visual["page_index"] == page_index and visual["source"] in ("online", "document")
                   for visual in item["slide_visuals"]):
            return None
        with self._lock:
            row = self._db.execute("""SELECT image FROM artifact_slide_visuals
                WHERE artifact_id = ? AND page_index = ?""", (artifact_id, page_index)).fetchone()
        return row["image"] if row else None

    def propose_edit(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                     instruction: str, model: CollaborationModel) -> dict | None:
        item = self.get(subject, artifact_id, documents)
        if item is None:
            return None
        if item["source_status"] != "current":
            raise SourceVersionChanged()
        if item["status"] != "pending" or item["mode"] != "standard":
            raise InvalidEdit()
        sections = item["section_drafts"]
        if not sections or len(sections) > 10:
            raise InvalidEdit()
        payload = {"instruction": instruction, "kind": item["kind"], "title": item["title"],
                   "sections": [{"index": index, "heading": section["heading"],
                                 "current_text": section.get("ai_text") or section["text"],
                                 "evidence_text": section["text"],
                                 "supporting_evidence": [source["text"] for source in section.get("supporting_sources", [])]}
                                for index, section in enumerate(sections)]}
        if len(json.dumps(payload, ensure_ascii=False)) > 32000:
            raise InvalidEdit()
        output = model.complete("artifact_edit", payload)
        if not isinstance(output, dict):
            raise InvalidEdit()
        index = output.get("section_index")
        revised = output.get("revised_text")
        quote = output.get("source_quote")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(sections):
            raise InvalidEdit()
        section = sections[index]
        evidence = [section["text"]] + [source["text"] for source in section.get("supporting_sources", [])]
        limit = 400 if item["kind"] == "docx" else 160
        if (section["evidence_gap"] or not isinstance(revised, str) or not revised.strip()
                or len(revised) > limit or not isinstance(quote, str) or not quote.strip()
                or len(quote) > 450 or not any(quote in text for text in evidence)
                or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127
                       for char in revised + quote)):
            raise InvalidEdit()
        before = section.get("ai_text") or section["text"]
        if revised.strip() == before.strip():
            raise InvalidEdit()
        with self._lock, self._db:
            current = self.get(subject, artifact_id, documents)
            if current is None:
                return None
            if current["source_status"] != "current" or current["draft_token"] != item["draft_token"]:
                raise DraftChanged()
            proposal_id = uuid4().hex
            self._db.execute("""INSERT INTO artifact_edit_proposals
                (id, org_id, owner_id, artifact_id, draft_token, section_index, before_text,
                 revised_text, source_quote, instruction, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (proposal_id, subject.org_id, subject.user_id, artifact_id, item["draft_token"],
                 index, before, revised.strip(), quote, instruction))
        return self.get_edit_proposal(subject, proposal_id, documents)

    def get_edit_proposal(self, subject: Subject, proposal_id: str,
                          documents: DocumentRepository) -> dict | None:
        with self._lock:
            row = self._db.execute("""SELECT * FROM artifact_edit_proposals
                WHERE id = ? AND org_id = ? AND owner_id = ?""",
                (proposal_id, subject.org_id, subject.user_id)).fetchone()
        if row is None:
            return None
        item = self.get(subject, row["artifact_id"], documents)
        if item is None:
            return None
        return {"id": row["id"], "artifact_id": row["artifact_id"], "artifact_title": item["title"],
                "kind": item["kind"], "section_index": row["section_index"],
                "heading": (item["outline"][row["section_index"]]
                            if row["section_index"] < len(item["outline"]) else "已变更章节"),
                "before_text": row["before_text"],
                "revised_text": row["revised_text"], "source_quote": row["source_quote"],
                "status": ("stale" if item["source_status"] != "current" else
                           "outdated" if row["status"] == "pending" and
                           (item["status"] != "pending" or item["draft_token"] != row["draft_token"])
                           else row["status"]),
                "draft_token": row["draft_token"]}

    def apply_edit_proposal(self, subject: Subject, proposal_id: str,
                            documents: DocumentRepository) -> dict | None:
        with self._lock, self._db:
            proposal = self.get_edit_proposal(subject, proposal_id, documents)
            if proposal is None:
                return None
            item = self.get(subject, proposal["artifact_id"], documents)
            if item is None:
                return None
            if item["source_status"] != "current":
                raise SourceVersionChanged()
            if proposal["status"] == "applied":
                return item
            if item["status"] != "pending" or item["draft_token"] != proposal["draft_token"]:
                raise DraftChanged()
            drafts = item["section_drafts"]
            section = drafts[proposal["section_index"]]
            section["ai_text"] = proposal["revised_text"]
            section["ai_quote"] = proposal["source_quote"]
            section.pop("ai_evidence", None)
            latest = self.get(subject, proposal["artifact_id"], documents)
            if latest is None or latest["source_status"] != "current":
                raise SourceVersionChanged()
            if latest["draft_token"] != proposal["draft_token"]:
                raise DraftChanged()
            self._db.execute("""UPDATE artifacts SET section_drafts = ?, reflection_trace = NULL,
                reflection_request_id = NULL WHERE id = ? AND org_id = ? AND owner_id = ? AND status = 'pending'""",
                (json.dumps(drafts, ensure_ascii=False), item["id"], subject.org_id, subject.user_id))
            self._db.execute("UPDATE artifact_edit_proposals SET status = 'applied' WHERE id = ?", (proposal_id,))
            return self.get(subject, item["id"], documents)

    def reflect(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                model: ReflectionModel, draft_token: str, request_id: str,
                limits: ReflectionLimits | None = None,
                presentation_style: str | None = None) -> dict | None:
        item = self.get(subject, artifact_id, documents)
        if item is None:
            return None
        if item["source_status"] != "current":
            raise SourceVersionChanged()
        with self._lock:
            row = self._db.execute("SELECT reflection_request_id FROM artifacts WHERE id = ?",
                                   (artifact_id,)).fetchone()
        if row and row["reflection_request_id"] == request_id:
            return item
        if item["status"] != "pending" or item["draft_token"] != draft_token:
            raise DraftChanged()
        if presentation_style in ("concise", "bullets"):
            item = item | {"presentation_style": presentation_style}
        result = run_reflection(item, model, limits=limits)
        with self._lock, self._db:
            current = self.get(subject, artifact_id, documents)
            if current is None:
                return None
            if current["source_status"] != "current" or current["draft_token"] != draft_token:
                raise DraftChanged()
            row = self._db.execute("SELECT reflection_request_id FROM artifacts WHERE id = ?",
                                   (artifact_id,)).fetchone()
            if row and row["reflection_request_id"] == request_id:
                return current
            self._db.execute("""UPDATE artifacts SET section_drafts = ?, reflection_trace = ?,
                reflection_request_id = ? WHERE id = ? AND org_id = ? AND owner_id = ? AND status = 'pending'""",
                (json.dumps(result["drafts"], ensure_ascii=False),
                 json.dumps(result["trace"], ensure_ascii=False), request_id,
                 artifact_id, subject.org_id, subject.user_id))
            return self.get(subject, artifact_id, documents)

    def collaborate(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                    model: CollaborationModel, draft_token: str, request_id: str) -> dict | None:
        item = self.get(subject, artifact_id, documents)
        if item is None:
            return None
        if item["source_status"] != "current":
            raise SourceVersionChanged()
        with self._lock:
            row = self._db.execute("SELECT collaboration_request_id FROM artifacts WHERE id = ?",
                                   (artifact_id,)).fetchone()
        if row and row["collaboration_request_id"] == request_id:
            return item
        if item["status"] != "pending" or item["draft_token"] != draft_token:
            raise DraftChanged()
        drafts, trace = run_collaboration(model, item)
        return self.commit_collaboration(subject, artifact_id, documents, drafts, trace,
                                         draft_token, request_id)

    def commit_collaboration(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                             drafts: list[dict], trace: list[dict], draft_token: str,
                             request_id: str) -> dict | None:
        with self._lock, self._db:
            current = self.get(subject, artifact_id, documents)
            if current is None:
                return None
            if current["source_status"] != "current":
                raise SourceVersionChanged()
            row = self._db.execute("SELECT collaboration_request_id FROM artifacts WHERE id = ?",
                                   (artifact_id,)).fetchone()
            if row and row["collaboration_request_id"] == request_id:
                return current
            if current["status"] != "pending" or current["draft_token"] != draft_token:
                raise DraftChanged()
            if (len(drafts) != len(current["section_drafts"]) or
                    any({key: value for key, value in draft.items() if key not in ("ai_quote", "ai_text", "ai_evidence")}
                        != original for draft, original in zip(drafts, current["section_drafts"]))):
                raise DraftChanged()
            self._db.execute("""UPDATE artifacts SET section_drafts = ?, collaboration_trace = ?,
                collaboration_request_id = ? WHERE id = ? AND org_id = ? AND owner_id = ? AND status = 'pending'""",
                (json.dumps(drafts, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), request_id,
                 artifact_id, subject.org_id, subject.user_id))
            return self.get(subject, artifact_id, documents)

    def save_notes(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                   notes: list[dict], draft_token: str) -> dict | None:
        with self._lock, self._db:
            item = self.get(subject, artifact_id, documents)
            if item is None:
                return None
            if item["source_status"] == "stale":
                raise SourceVersionChanged()
            if item["status"] != "pending":
                raise DraftChanged()
            drafts = item["section_drafts"]
            if (draft_token != item["draft_token"] or len(notes) != len(drafts)
                    or any(note["heading"] != draft["heading"] for note, draft in zip(notes, drafts))):
                raise DraftChanged()
            for draft, note in zip(drafts, notes):
                value = note["text"].strip()
                if (len(value) > (160 if item["kind"] == "pptx" else 1200)
                        or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value)):
                    raise InvalidNote()
                if value:
                    draft["manual_note"] = value
                    draft["manual_note_by"] = subject.user_id
                else:
                    draft.pop("manual_note", None)
                    draft.pop("manual_note_by", None)
            current = self._visible_sources(subject, documents, item["source_ids"])
            if current is None:
                return None
            if {record.document_id: record.version for record, _ in current} != item["source_versions"]:
                raise SourceVersionChanged()
            self._db.execute("""UPDATE artifacts SET section_drafts = ?
                WHERE id = ? AND org_id = ? AND owner_id = ? AND status = 'pending'""",
                (json.dumps(drafts, ensure_ascii=False), artifact_id, subject.org_id, subject.user_id))
            return self.get(subject, artifact_id, documents)

    def preview(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                outline: list[str]) -> dict | None:
        with self._lock, self._db:
            item = self.get(subject, artifact_id, documents)
            if item is None:
                return None
            if item["source_status"] == "stale":
                raise SourceVersionChanged()
            if item["status"] == "completed":
                return item
            sources = self._visible_sources(subject, documents, item["source_ids"])
            if sources is None:
                return None
            drafts = self._make_drafts(outline, sources, item["mode"])
            latest = self._visible_sources(subject, documents, item["source_ids"])
            current_versions = {record.document_id: record.version for record, _ in sources}
            if latest is None or {record.document_id: record.version for record, _ in latest} != current_versions:
                raise SourceVersionChanged()
            self._db.execute("UPDATE artifacts SET outline = ?, section_drafts = ?, collaboration_trace = NULL, collaboration_request_id = NULL, composition_plan = NULL WHERE id = ? AND status = 'pending'",
                             (json.dumps(outline), json.dumps(drafts, ensure_ascii=False), artifact_id))
            self._db.execute("DELETE FROM artifact_slide_visuals WHERE artifact_id = ?", (artifact_id,))
            return self.get(subject, artifact_id, documents)

    def list_for(self, subject: Subject, documents: DocumentRepository) -> list[dict]:
        with self._lock:
            ids = [row["id"] for row in self._db.execute(
                "SELECT id FROM artifacts WHERE org_id = ? AND owner_id = ? ORDER BY rowid DESC",
                (subject.org_id, subject.user_id),
            ).fetchall()]
        return [item for artifact_id in ids if (item := self.get(subject, artifact_id, documents)) is not None]

    def review(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
               reviews: list[dict]) -> dict | None:
        with self._lock, self._db:
            item = self.get(subject, artifact_id, documents)
            if item is None:
                return None
            if item["source_status"] == "stale":
                raise SourceVersionChanged()
            if item["mode"] != "comparison" or item["status"] != "pending":
                raise InvalidReview()
            drafts = item["section_drafts"]
            if len(reviews) != len(drafts) or any(review["heading"] != draft["heading"]
                                                   for review, draft in zip(reviews, drafts)):
                raise DraftChanged()
            for draft, review in zip(drafts, reviews):
                decision = review["decision"]
                note = review["note"].strip()
                if (decision not in ("aligned", "different", "unresolved")
                        or (draft["evidence_gap"] and decision != "unresolved")
                        or (decision == "different" and not note)):
                    raise InvalidReview()
                draft["review"] = {"decision": decision, "note": note,
                                   "reviewed_by": subject.user_id,
                                   "reviewed_at": datetime.now(timezone.utc).isoformat()}
            sources = self._visible_sources(subject, documents, item["source_ids"])
            if sources is None:
                return None
            if {record.document_id: record.version for record, _ in sources} != item["source_versions"]:
                raise SourceVersionChanged()
            self._db.execute("UPDATE artifacts SET section_drafts = ? WHERE id = ? AND status = 'pending'",
                             (json.dumps(drafts, ensure_ascii=False), artifact_id))
            return self.get(subject, artifact_id, documents)

    def confirm(self, subject: Subject, artifact_id: str, documents: DocumentRepository,
                outline: list[str], draft_token: str | None = None) -> dict | None:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM artifacts WHERE id = ? AND org_id = ? AND owner_id = ?",
                                   (artifact_id, subject.org_id, subject.user_id)).fetchone()
            if row is None:
                return None
            sources = self._visible_sources(subject, documents, json.loads(row["source_ids"]))
            if sources is None:
                return None
            current_versions = {record.document_id: record.version for record, _ in sources}
            saved_versions = json.loads(row["source_versions"]) if row["source_versions"] is not None else None
            if saved_versions is not None and saved_versions != current_versions:
                raise SourceVersionChanged()
            if row["status"] == "pending":
                if row["section_drafts"] is not None and outline != json.loads(row["outline"]):
                    raise DraftChanged()
                drafts = (json.loads(row["section_drafts"]) if row["section_drafts"] is not None
                          else self._make_drafts(outline, sources, row["mode"]))
                if draft_token is not None and draft_token != self._draft_token(drafts):
                    raise DraftChanged()
                if row["mode"] == "comparison" and any("review" not in draft for draft in drafts):
                    raise ReviewRequired()
                visual_rows = self._db.execute("SELECT * FROM artifact_slide_visuals WHERE artifact_id = ?",
                                               (artifact_id,)).fetchall()
                visuals = {visual["page_index"]: {"source": visual["source"], "asset_name": visual["asset_name"],
                           "image": visual["image"], "photographer": visual["photographer"],
                           "page_url": visual["page_url"]}
                           for visual in visual_rows if visual["page_index"] < len(drafts)
                           and visual["basis"] == visual_basis(drafts[visual["page_index"]])}
                content = make_file(row["kind"], row["title"], row["goal"], drafts, row["theme"], visuals,
                                    brand_name=organization_brand(row["org_id"]))
                latest = self._visible_sources(subject, documents, json.loads(row["source_ids"]))
                if latest is None or {record.document_id: record.version for record, _ in latest} != current_versions:
                    raise SourceVersionChanged()
                self._db.execute("""UPDATE artifacts SET outline = ?, status = ?, content = ?, source_versions = ?, section_drafts = ?
                    WHERE id = ?""", (json.dumps(outline), "completed", content,
                                        json.dumps(current_versions), json.dumps(drafts, ensure_ascii=False), artifact_id))
        return self.get(subject, artifact_id, documents)

    def download(self, subject: Subject, artifact_id: str, documents: DocumentRepository) -> tuple[str, bytes] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM artifacts WHERE id = ? AND org_id = ? AND owner_id = ? AND status = ?",
                                   (artifact_id, subject.org_id, subject.user_id, "completed")).fetchone()
        if row is None:
            return None
        sources = self._visible_sources(subject, documents, json.loads(row["source_ids"]))
        if sources is None:
            return None
        if row["source_versions"] is not None and json.loads(row["source_versions"]) != {
            record.document_id: record.version for record, _ in sources
        }:
            raise SourceVersionChanged()
        return row["kind"], row["content"]


def make_file(kind: str, title: str, goal: str, drafts: list[dict], theme: str = "brief",
              visuals: dict[int, dict] | None = None, brand_name: str = "") -> bytes:
    stream = io.BytesIO()
    if kind == "docx":
        document = Document()
        section = document.sections[0]
        section.top_margin = DocumentInches(0.8)
        section.bottom_margin = DocumentInches(0.8)
        document.styles["Normal"].font.name = "Microsoft YaHei"
        document.styles["Normal"].font.size = Pt(10)
        document.add_heading(title, 0)
        document.add_paragraph(goal)
        for draft in drafts:
            document.add_heading(draft["heading"], level=1)
            if "sources" in draft:
                for source in draft["sources"]:
                    document.add_heading(f"{source['title']} {source['version']}", level=2)
                    document.add_paragraph(source["text"] if not source["evidence_gap"]
                                           else "待补充证据：此来源未找到与本主题直接相关的片段。")
                    if citation := source["citation"]:
                        document.add_paragraph(f"来源：{citation['title']} {citation['version']} · {citation['location']}")
                review = draft.get("review")
                if review:
                    label = {"aligned": "无显著差异", "different": "发现差异", "unresolved": "证据不足，待核对"}[review["decision"]]
                    document.add_paragraph(f"人工核对结论：{label}")
                    if review["note"]:
                        document.add_paragraph(f"人工备注：{review['note']}")
                    document.add_paragraph(f"核对人：{review['reviewed_by']} · {review['reviewed_at']}")
                else:
                    document.add_paragraph("核对结论：待人工核对。")
                if draft.get("manual_note"):
                    document.add_paragraph(f"人工补充（{draft['manual_note_by']}）：{draft['manual_note']}")
                continue
            document.add_paragraph(draft["text"] if not draft["evidence_gap"] else "待补充证据：所选资料未找到与本章节直接相关的片段。")
            if draft.get("ai_text"):
                document.add_paragraph(f"AI 协作草稿（待人工核对）：{draft['ai_text']}")
                if draft.get("ai_evidence"):
                    for evidence in draft["ai_evidence"]:
                        citation = evidence["citation"]
                        document.add_paragraph(f"模型依据原文：{evidence['quote']}")
                        document.add_paragraph(f"依据来源：{citation['title']} {citation['version']} · {citation['location']}")
                else:
                    document.add_paragraph(f"模型依据原文：{draft['ai_quote']}")
            if citation := draft["citation"]:
                document.add_paragraph(f"来源：{citation['title']} {citation['version']} · {citation['location']}")
            if draft.get("manual_note"):
                document.add_paragraph(f"人工补充（{draft['manual_note_by']}）：{draft['manual_note']}")
        document.save(stream)
    else:
        deck = Presentation()
        deck.slide_width = Inches(13.33)
        deck.slide_height = Inches(7.5)
        strategic = theme == "strategy"
        accent = RGBColor(239, 184, 105) if strategic else RGBColor(215, 91, 72)
        for page_number, draft in enumerate(drafts, start=1):
            layout = _slide_layout(page_number - 1, len(drafts))
            reverse = layout == "split" and page_number % 4 == 0
            visual = (visuals or {}).get(page_number - 1, {})
            photo = visual.get("image") or visual.get("asset_name")
            contain_photo = visual.get("source") == "document"
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            if layout == "cover":
                background = RGBColor(37, 62, 73) if strategic else RGBColor(232, 242, 235)
            elif layout == "focus":
                background = RGBColor(235, 241, 236) if strategic else RGBColor(24, 65, 62)
            elif layout == "closing":
                background = RGBColor(31, 48, 52) if strategic else RGBColor(25, 55, 53)
            elif reverse:
                background = RGBColor(237, 241, 235) if strategic else RGBColor(248, 245, 237)
            else:
                background = RGBColor(37, 62, 73) if strategic else RGBColor(246, 249, 244)
            dark_page = layout == "closing" or (layout == "focus" and not strategic) or (
                strategic and layout in ("cover", "split") and not reverse)
            ink = RGBColor(246, 249, 245) if dark_page else RGBColor(26, 65, 58)
            muted = RGBColor(188, 211, 202) if dark_page else RGBColor(87, 119, 106)
            number_color = RGBColor(73, 112, 106) if dark_page else RGBColor(191, 212, 198)
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = background
            if layout == "cover":
                _slide_rect(slide, 7.04, 0.58, 6.29, 5.77, accent)
                _slide_photo(slide, photo or "workspace.jpg", 7.22, 0.30, 6.11, 5.78, contain_photo)
                text_left, text_width = 0.9, 5.76
            elif layout == "split":
                if reverse:
                    _slide_rect(slide, 0.28, 0.34, 5.45, 5.90, accent)
                    _slide_photo(slide, photo or "planning.jpg", 0.48, 0.55, 5.23, 5.70, contain_photo)
                    text_left, text_width = 6.28, 6.05
                else:
                    _slide_rect(slide, 7.48, 0.42, 5.50, 5.80, accent)
                    _slide_photo(slide, photo or "team.jpg", 7.68, 0.62, 5.22, 5.63, contain_photo)
                    text_left, text_width = 0.9, 5.97
            elif layout == "closing":
                _slide_photo(slide, photo or "planning.jpg", 0, 0, 5.18, 6.35, contain_photo)
                _slide_rect(slide, 5.18, 0, 0.14, 6.35, accent)
                text_left, text_width = 5.92, 6.40
            else:
                _slide_rect(slide, 0, 0, 0.22, 6.35, accent)
                _slide_rect(slide, 10.95, 0, 2.38, 6.35,
                            RGBColor(34, 80, 75) if dark_page else RGBColor(209, 226, 212))
                text_left, text_width = 1.12, 9.40
            if brand_name:
                brand_label = _slide_fit_label(brand_name, 28)
                if layout == "closing" or reverse:
                    badge_width = min(3.4, max(1.15, 0.52 + _slide_text_units(brand_label) * 0.1))
                    _slide_rect(slide, 0.64, 0.18, badge_width, 0.47, RGBColor(25, 55, 53))
                _slide_label(slide, brand_label, 0.9, 0.25,
                             2.8, 0.32, 14,
                             RGBColor(246, 249, 245) if layout == "closing" or reverse else ink, True)
            _slide_label(slide, f"{page_number:02d}", text_left, 0.50,
                         2.05, 1.05, 65 if layout != "focus" else 80, number_color, True)
            _slide_rect(slide, text_left, 1.55, 0.72, 0.06, accent)
            _slide_label(slide, _slide_fit_label(f"{title} · {goal}", int(text_width * 5.5)), text_left, 1.73,
                         text_width, 0.32, 11, muted)
            heading = slide.shapes.title
            heading.left, heading.top = Inches(text_left), Inches(2.20)
            heading.width, heading.height = Inches(text_width), Inches(1.35)
            heading.text = draft["heading"]
            heading.text_frame.word_wrap = True
            heading.text_frame.margin_left = heading.text_frame.margin_right = Inches(0)
            heading.text_frame.margin_top = heading.text_frame.margin_bottom = Inches(0)
            heading_units = _slide_text_units(draft["heading"])
            heading_size = (24 if heading_units > 45 else 29 if heading_units > 31 else
                            34 if heading_units > 21 else 40 if layout == "focus" else 38)
            heading.text_frame.paragraphs[0].font.name = "Microsoft YaHei"
            heading.text_frame.paragraphs[0].font.size = SlidePt(heading_size)
            heading.text_frame.paragraphs[0].font.bold = True
            heading.text_frame.paragraphs[0].font.color.rgb = ink
            heading.text_frame.paragraphs[0].alignment = PP_ALIGN.LEFT
            body_shape = slide.placeholders[1]
            body = body_shape.text_frame
            content = (draft.get("ai_text") or
                       (draft["text"] if not draft["evidence_gap"] else "待补充证据：本页没有匹配片段。"))
            if draft.get("manual_note"):
                content += f"\n人工补充（{draft['manual_note_by']}）：{draft['manual_note']}"
            lines = [line.strip().lstrip("•-* ").strip() for line in content.splitlines() if line.strip()]
            longest_line = max((len(line) for line in lines), default=0)
            body_size = (13 if len(content) > 245 or longest_line > 36 else
                         14 if longest_line > 24 else
                         15 if len(content) > 150 or longest_line > 19 else
                         18 if len(lines) <= 4 else 16)
            body.clear()
            body_top = 3.78 if heading_units > int(text_width * 5.2) else 3.60
            body_shape.left, body_shape.top = Inches(text_left), Inches(body_top)
            body_shape.width, body_shape.height = Inches(text_width), Inches(6.08 - body_top)
            body.word_wrap = True
            body.vertical_anchor = MSO_ANCHOR.TOP
            body.margin_left = body.margin_right = Inches(0)
            body.margin_top = Inches(0)
            for index, line in enumerate(lines or [content]):
                paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
                paragraph.text = line
                paragraph.level = 0
                paragraph.alignment = PP_ALIGN.LEFT
                paragraph_properties = paragraph._p.get_or_add_pPr()
                paragraph_properties.set("marL", "0")
                paragraph_properties.set("indent", "0")
                paragraph_properties.insert(0, OxmlElement("a:buNone"))
                paragraph.space_after = SlidePt(11 if draft.get("ai_text") else 0)
                paragraph.font.name = "Microsoft YaHei"
                paragraph.font.size = SlidePt(body_size)
                paragraph.font.color.rgb = ink
            # Placeholders originate in the slide layout; move them above photos and bands.
            for placeholder in (heading, body_shape):
                slide.shapes._spTree.remove(placeholder._element)
                slide.shapes._spTree.append(placeholder._element)
            _slide_rect(slide, 0, 6.35, 13.33, 1.15, background)
            _slide_rect(slide, 0.9, 6.43, 11.55, 0.012, muted)
            counter = slide.shapes.add_textbox(Inches(11.7), Inches(6.65), Inches(0.7), Inches(0.4))
            counter.text_frame.text = f"{page_number:02d} / {len(drafts):02d}"
            counter.text_frame.paragraphs[0].font.size = SlidePt(10)
            counter.text_frame.paragraphs[0].font.color.rgb = muted
            if visual.get("source") == "online" and visual.get("photographer"):
                credit = slide.shapes.add_textbox(Inches(9.4), Inches(7.05), Inches(3.0), Inches(0.25))
                credit.text_frame.text = f"{visual['photographer']} / Unsplash"
                credit.text_frame.paragraphs[0].font.size = SlidePt(7)
                credit.text_frame.paragraphs[0].font.color.rgb = muted
                if visual.get("page_url"):
                    credit.text_frame.paragraphs[0].runs[0].hyperlink.address = visual["page_url"]
        deck.save(stream)
    return stream.getvalue()
