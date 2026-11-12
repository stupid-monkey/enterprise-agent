"""Grounded slide themes and bounded public photo retrieval."""

import io
import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from docx import Document
from PIL import Image, ImageOps, UnidentifiedImageError
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfReader

from app.api.content import search_terms


@dataclass(frozen=True)
class VisualTheme:
    name: str
    query: str
    local_asset: str
    terms: tuple[str, ...]


# Search strings are fixed here. Document titles, excerpts and model output never leave in a photo query.
THEMES = (
    VisualTheme("security", "cybersecurity access control", "security.jpg",
                ("权限", "安全", "访问控制", "保密", "身份认证", "登录", "security", "access control")),
    VisualTheme("data", "data analytics technology", "data.jpg",
                ("数据", "分析", "指标", "报表", "检索", "知识库", "搜索", "data", "analytics")),
    VisualTheme("documents", "business documents workspace", "documents.jpg",
                ("文档", "资料", "文件", "报告", "制度", "合同", "document", "paperwork")),
    VisualTheme("product", "product design collaboration", "planning.jpg",
                ("产品", "需求", "设计", "迭代", "研发", "上线", "product", "design")),
    VisualTheme("planning", "team planning whiteboard", "planning.jpg",
                ("计划", "规划", "路线图", "目标", "实施", "进展", "风险", "复盘", "流程", "审批", "planning", "roadmap")),
    VisualTheme("finance", "financial planning office", "finance.jpg",
                ("财务", "预算", "成本", "报销", "采购", "finance", "budget")),
    VisualTheme("people", "team collaboration office", "team.jpg",
                ("员工", "客户", "团队", "会议", "培训", "协作", "部门", "team", "meeting")),
)
DEFAULT_THEME = VisualTheme("workspace", "modern office workspace", "workspace.jpg", ())
LOCAL_ASSETS = ("planning.jpg", "data.jpg", "team.jpg", "workspace.jpg",
                "documents.jpg", "finance.jpg")


def _safe_embedded_image(raw: bytes) -> bytes | None:
    if not raw or len(raw) > 4_000_000:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if (source.format not in ("JPEG", "PNG", "WEBP") or source.width < 400
                    or source.height < 200 or source.width * source.height > 20_000_000):
                return None
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((1800, 1400))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def embedded_visuals(file_type: str, content: bytes) -> list[dict]:
    """Extract bounded, local pictures with nearby text for slide matching."""
    if len(content) > 8_000_000 or file_type not in ("DOCX", "PPTX", "PDF"):
        return []
    found: list[dict] = []

    def add(raw: bytes, location: str, context: str) -> None:
        if len(found) >= 12:
            return
        image = _safe_embedded_image(raw)
        if image is not None:
            found.append({"image": image, "location": location,
                          "context": context[:1200], "image_id": hashlib.sha256(image).hexdigest()})

    try:
        if file_type == "DOCX":
            document = Document(io.BytesIO(content))
            paragraphs = document.paragraphs
            for index, paragraph in enumerate(paragraphs):
                context = " ".join(part.text for part in paragraphs[max(0, index - 2):index + 3])
                for blip in paragraph._p.xpath('.//a:blip'):
                    relationship = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                    if relationship and relationship in document.part.related_parts:
                        add(document.part.related_parts[relationship].blob, f"段落 {index + 1}", context)
        elif file_type == "PPTX":
            deck = Presentation(io.BytesIO(content))
            for index, slide in enumerate(deck.slides):
                context = " ".join(shape.text for shape in slide.shapes if shape.has_text_frame)
                for shape in slide.shapes:
                    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                        add(shape.image.blob, f"第 {index + 1} 页", context)
        else:
            reader = PdfReader(io.BytesIO(content))
            for index, page in enumerate(reader.pages[:100]):
                context = page.extract_text() or ""
                for picture in page.images:
                    add(picture.data, f"第 {index + 1} 页", context)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return found
    return found


def best_document_visual(heading: str, candidates: list[dict], used: set[str]) -> dict | None:
    terms = search_terms(heading)
    if not terms:
        return None
    ranked = []
    for candidate in candidates:
        if candidate["image_id"] in used:
            continue
        context = candidate["context"].lower()
        score = sum(1 for term in terms if term in context)
        if score:
            ranked.append((score, candidate))
    return max(ranked, key=lambda pair: pair[0])[1] if ranked else None


def visual_theme(title: str, draft: dict) -> VisualTheme:
    title_text = title.lower()
    heading = str(draft.get("heading") or "").lower()
    body = str(draft.get("ai_text") or draft.get("text") or "").lower()
    scores = [(sum(title_text.count(term) + 8 * heading.count(term) + min(body.count(term), 2)
                   for term in theme.terms), theme)
              for theme in THEMES]
    score, theme = max(scores, key=lambda entry: entry[0])
    return theme if score else DEFAULT_THEME


def visual_basis(draft: dict) -> str:
    import hashlib
    import json
    data = {"heading": draft.get("heading"), "body": draft.get("ai_text") or draft.get("text"),
            "evidence_gap": draft.get("evidence_gap")}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def local_asset_for(theme: VisualTheme, used: set[str]) -> str:
    for asset in (theme.local_asset, *LOCAL_ASSETS):
        if asset not in used:
            return asset
    return theme.local_asset


class UnsplashPhotoProvider:
    """Uses the official API; never fetches a URL supplied by the user or model."""

    def __init__(self, access_key: str, client: httpx.Client | None = None):
        self.access_key = access_key
        self.client = client or httpx.Client(timeout=8.0, follow_redirects=False)

    def find(self, query: str, excluded_ids: set[str]) -> dict | None:
        if query not in {theme.query for theme in (*THEMES, DEFAULT_THEME)}:
            raise ValueError("UNAPPROVED_VISUAL_QUERY")
        response = self.client.get("https://api.unsplash.com/search/photos", params={
            "query": query, "orientation": "landscape", "content_filter": "high", "per_page": 8,
        }, headers={"Authorization": f"Client-ID {self.access_key}",
                    "Accept-Version": "v1"})
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            photo_id = result.get("id")
            urls = result.get("urls") if isinstance(result.get("urls"), dict) else {}
            links = result.get("links") if isinstance(result.get("links"), dict) else {}
            raw_url = urls.get("regular", "")
            page_url = links.get("html", "")
            width, height = result.get("width"), result.get("height")
            if (not isinstance(photo_id, str) or photo_id in excluded_ids
                    or not isinstance(raw_url, str) or not isinstance(page_url, str)
                    or urlparse(raw_url).scheme != "https"
                    or urlparse(raw_url).hostname != "images.unsplash.com"
                    or urlparse(page_url).scheme != "https"
                    or urlparse(page_url).hostname != "unsplash.com"
                    or not isinstance(width, int) or not isinstance(height, int)
                    or width < 1000 or height < 600):
                continue
            with self.client.stream("GET", raw_url, follow_redirects=False) as image_response:
                image_response.raise_for_status()
                if not image_response.headers.get("content-type", "").lower().startswith("image/"):
                    continue
                chunks = []
                size = 0
                for chunk in image_response.iter_bytes():
                    size += len(chunk)
                    if size > 4_000_000:
                        break
                    chunks.append(chunk)
                if size > 4_000_000:
                    continue
            try:
                with Image.open(io.BytesIO(b"".join(chunks))) as source:
                    if (source.format not in ("JPEG", "PNG", "WEBP") or source.width < 1000
                            or source.height < 600 or source.width * source.height > 20_000_000):
                        continue
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.thumbnail((1800, 1400))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=84, optimize=True)
            except (UnidentifiedImageError, OSError, ValueError):
                continue
            download_location = links.get("download_location", "")
            if (isinstance(download_location, str) and urlparse(download_location).scheme == "https"
                    and urlparse(download_location).hostname == "api.unsplash.com"):
                self.client.get(download_location, headers={"Authorization": f"Client-ID {self.access_key}"},
                                follow_redirects=False).raise_for_status()
            user = result.get("user") if isinstance(result.get("user"), dict) else {}
            photographer = user.get("name", "")
            return {"image": output.getvalue(), "photo_id": photo_id,
                    "photographer": str(photographer)[:80], "page_url": page_url,
                    "provider": "Unsplash", "license_url": "https://unsplash.com/license"}
        return None
