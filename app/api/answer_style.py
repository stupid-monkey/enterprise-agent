"""Convert a user's low-trust free-text preference to a bounded display style."""

import re
import unicodedata
from typing import Literal

AnswerStyle = Literal["concise", "bullets"]

_ALIASES: dict[str, AnswerStyle] = {
    "简短回答": "concise",
    "简洁回答": "concise",
    "请简短回答": "concise",
    "请简洁回答": "concise",
    "回答尽量简洁": "concise",
    "以后请简短回答": "concise",
    "以后回答尽量简洁": "concise",
    "以后回答请简短一些": "concise",
    "分点回答": "bullets",
    "请分点回答": "bullets",
    "以后请分点回答": "bullets",
    "回答时请分点": "bullets",
    "请用要点回答": "bullets",
    "以后请用要点回答": "bullets",
}


def classify_answer_style(preference: str | None) -> AnswerStyle | None:
    """Only exact, short phrases become instructions; all other text is ignored."""
    if not isinstance(preference, str) or len(preference) > 40:
        return None
    normalized = unicodedata.normalize("NFKC", preference)
    normalized = re.sub(r"[\s，,。.!！]+", "", normalized)
    return _ALIASES.get(normalized)


def format_grounded_paragraphs(paragraphs: list[str], citations: list,
                               style: AnswerStyle | None) -> list[str]:
    """Preserve every cited fact; only add a visual bullet when requested."""
    if style is None or not citations or len(paragraphs) != len(citations):
        return paragraphs
    if style == "bullets":
        return [f"• {paragraph}" if not paragraph.startswith("• ") else paragraph
                for paragraph in paragraphs]
    # Extractive answers are already bounded upstream. Shortening a cited paragraph
    # here could drop a required clause in a multi-part question.
    return paragraphs
