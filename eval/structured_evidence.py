"""Conservative shape checks for comparison and numeric source quotes.

This module does not establish semantic entailment. It only rejects some
obvious attribute, unit, period, and bound mismatches in extractive evidence.
"""

import re


_NUMBER = r"[零〇一二两三四五六七八九十百千万亿\d]+"
_COMPARISON = re.compile(
    r"(?P<left>[\u4e00-\u9fffA-Za-z0-9]{2,12})[和与]"
    r"(?P<right>[\u4e00-\u9fffA-Za-z0-9]{2,12})的"
    r"(?P<attribute>[\u4e00-\u9fffA-Za-z0-9]{2,20}?)"
    r"(?:一样|相同|不同|区别|差异)"
)
_PERIODS = ("每月", "每次", "每周", "每天", "每日", "每年", "每晚", "每季度")
_BOUND = re.compile(r"(?:最多|上限|不超过|不得超过|至多|最长)")
_QUANTITY = re.compile(r"(?:多少|几(?:天|日|周|月|年|小时|分钟|分|次|元))")


def _sentences(quote: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？；;\n]", quote) if part.strip()]


def _unit_pattern(question: str) -> re.Pattern[str]:
    if re.search(r"(?:多少分|几分|及格分|合格分)", question):
        return re.compile(_NUMBER + r"分(?!钟)")
    if "几次" in question:
        return re.compile(_NUMBER + r"次")
    if re.search(r"(?:额度|多少钱|多少元|报多少钱|报多少)", question):
        return re.compile(_NUMBER + r"(?:万元|亿元|元)")
    return re.compile(_NUMBER + r"(?:个工作日|工作日|天|日|周|月|年|小时|分钟|分|次|万元|亿元|元|%|％)")


def structured_evidence_check(question: str, quote: str | None) -> str:
    """Return a reason code; `shape_match` is not a supported-answer verdict."""
    if not quote:
        return "no_quote"
    sentences = _sentences(quote)
    comparison = _COMPARISON.search(question)
    if comparison:
        left, right, attribute = comparison.group("left", "right", "attribute")
        both = [sentence for sentence in sentences if left in sentence and right in sentence]
        if not both:
            return "missing_comparison_entities"
        if not any(attribute in sentence for sentence in both):
            return "missing_attribute"
        return "shape_match"
    if not _QUANTITY.search(question):
        return "unchecked"
    unit = _unit_pattern(question)
    amount_sentences = [sentence for sentence in sentences if unit.search(sentence)]
    if not amount_sentences:
        return "missing_unit"
    period = next((value for value in _PERIODS if value in question), None)
    if period and not any(period in sentence for sentence in amount_sentences):
        return "missing_period"
    if _BOUND.search(question) and not any(
        _BOUND.search(sentence) for sentence in amount_sentences
        if not period or period in sentence
    ):
        return "missing_bound"
    return "shape_match"
