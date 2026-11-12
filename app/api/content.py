"""Bounded local parsing and extractive answers for the first usable workflow."""

import io
import re
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError as DocxPackageNotFoundError
from pptx import Presentation
from pptx.exc import PackageNotFoundError as PptxPackageNotFoundError
from pypdf import PdfReader
from pypdf.errors import PdfReadError


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
SUPPORTED_TYPES = {".txt": "TXT", ".md": "MD", ".pdf": "PDF", ".docx": "DOCX", ".pptx": "PPTX"}
QUESTION_FILLERS = ("这个", "那个", "什么", "哪些", "怎么", "如何", "是否", "多少", "时候",
                    "可以", "请问", "请概括", "概括", "资料", "文件", "内容", "关于", "有关",
                    "介绍", "一下", "需要", "的是", "是", "的", "有", "吗", "呢", "公司",
                    "如果", "想要", "最多", "我")


def extract_text(filename: str, content: bytes) -> tuple[str, str]:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_type = SUPPORTED_TYPES.get(suffix)
    if file_type is None or not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("INVALID_FILE")
    try:
        if file_type in ("TXT", "MD"):
            text = content.decode("utf-8-sig")
        elif file_type == "PDF":
            reader = PdfReader(io.BytesIO(content))
            if len(reader.pages) > 100:
                raise ValueError("INVALID_FILE")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif file_type == "DOCX":
            document = Document(io.BytesIO(content))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        else:
            slides = Presentation(io.BytesIO(content))
            text = "\n".join(shape.text for slide in slides.slides for shape in slide.shapes if shape.has_text_frame)
    except (UnicodeError, OSError, KeyError, TypeError, ValueError, BadZipFile,
            DocxPackageNotFoundError, PptxPackageNotFoundError, PdfReadError) as exc:
        raise ValueError("INVALID_FILE") from exc
    text = text.strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError("UNREADABLE_FILE")
    return file_type, text


def search_terms(value: str, *, question: bool = False) -> set[str]:
    normalized = value.lower()
    if question:
        for phrase in QUESTION_FILLERS:
            normalized = normalized.replace(phrase, " ")
        # "请年假" is a request verb plus topic; preserve "请假" as a topic.
        normalized = re.sub(r"请(?=[\u4e00-\u9fff]{1,4}假)", " ", normalized)
    words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]{3,}", normalized)
    terms = {word[index:index + 2] for word in words if re.fullmatch(r"[\u4e00-\u9fff]+", word)
             for index in range(len(word) - 1)}
    terms.update(word for word in words if re.fullmatch(r"[a-zA-Z0-9]{3,}", word))
    return terms


def split_segments(value: str, *, max_chars: int = 500) -> list[str]:
    segments: list[str] = []
    heading = ""
    for block in re.split(r"\n\s*\n+", value):
        block = block.strip()
        if not block:
            continue
        if re.fullmatch(r"#{1,6}\s+[^\n]+", block):
            heading = block
            continue
        current = heading
        heading = ""
        for sentence in (part.strip() for part in re.split(r"(?<=[。！？.!?；;])|\n+", block)):
            if not sentence:
                continue
            for start in range(0, len(sentence), max_chars):
                part = sentence[start:start + max_chars]
                if current and len(current) + len(part) + 1 > max_chars:
                    segments.append(current)
                    current = ""
                current = f"{current} {part}".strip()
        if current:
            segments.append(current)
    if heading:
        segments.append(heading)
    return segments


def relevant_sentences(question: str, text: str, *, limit: int = 3) -> list[str]:
    terms = search_terms(question, question=True)
    if not terms:
        return []
    sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?；;])|\n+", text) if part.strip()]
    scored = []
    for index, sentence in enumerate(sentences):
        score = sum(1 for term in terms if term in sentence.lower())
        if score >= min(2, len(terms)):
            scored.append((-score, index, sentence[:400]))
    return [sentence for _, _, sentence in sorted(scored)[:limit]]
