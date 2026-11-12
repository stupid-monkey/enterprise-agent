"""Small, deterministic probes for obvious gaps in extractive evidence.

These checks are necessary conditions for a few question shapes, not an
entailment judge. They must not be used as a general answer-quality score.
"""

import re


_COMPARISON = re.compile(
    r"(?P<left>[\u4e00-\u9fffA-Za-z0-9]{2,12})[和与]"
    r"(?P<right>[\u4e00-\u9fffA-Za-z0-9]{2,12})的.{0,20}"
    r"(?:一样|相同|不同|区别|差异)"
)
_QUANTITY_QUESTION = re.compile(r"(?:多少|几(?:天|日|周|月|年|小时|分钟|分|次|元))")
_QUANTITY_EVIDENCE = re.compile(
    r"[零〇一二两三四五六七八九十百千万亿\d]+"
    r"(?:个工作日|工作日|天|日|周|月|年|小时|分钟|分|次|万元|亿元|元|%|％)"
)
_MONEY_EVIDENCE = re.compile(r"[零〇一二两三四五六七八九十百千万亿\d]+(?:万元|亿元|元)")
_COUNT_EVIDENCE = re.compile(r"[零〇一二两三四五六七八九十百千万亿\d]+次")


def evidence_coverage(question: str, quote: str | None) -> str:
    """Return a visible failure reason, or `unchecked` for other question shapes."""
    if not quote:
        return "no_quote"
    comparison = _COMPARISON.search(question)
    if comparison and not all(entity in quote for entity in comparison.group("left", "right")):
        return "missing_comparison_entities"
    if _QUANTITY_QUESTION.search(question):
        if re.search(r"(?:额度|多少钱|多少元|报多少钱|报多少)", question):
            quantity = _MONEY_EVIDENCE
        elif "几次" in question:
            quantity = _COUNT_EVIDENCE
        else:
            quantity = _QUANTITY_EVIDENCE
        if not quantity.search(quote):
            return "missing_quantity"
    if comparison or _QUANTITY_QUESTION.search(question):
        return "surface_covered"
    return "unchecked"
