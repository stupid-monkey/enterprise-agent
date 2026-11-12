"""Conservative shadow-candidate gate and authoritative source recheck."""

from collections.abc import Mapping, Sequence

from app.api.documents import DocumentRepository, SearchHit
from app.security.policy import Subject


def calibrate_distance_gate(rows: list[dict]) -> float:
    """Choose a conservative cutoff from labeled calibration rows only."""
    candidates = sorted({score["vector_distance"] for row in rows
                         for score in row["hybrid_scores"][:1]})
    if not candidates:
        return 0.0
    thresholds = [0.0] + [(left + right) / 2 for left, right in zip(candidates, candidates[1:])]
    thresholds.append(candidates[-1] + 0.0001)

    def quality(threshold: float) -> tuple[int, int, float]:
        correct = sum((row["hybrid_scores"][0]["document_id"]
                       if row["hybrid_scores"] and row["hybrid_scores"][0]["vector_distance"] <= threshold
                       else None) == row["expected_id"] for row in rows)
        negative_rejections = sum(not row["hybrid_scores"] or
                                  row["hybrid_scores"][0]["vector_distance"] > threshold
                                  for row in rows if row["expected_id"] is None)
        return correct, negative_rejections, -threshold

    return round(max(thresholds, key=quality), 4)


def resolve_gated_candidates(documents: DocumentRepository, subject: Subject,
                             candidates: Sequence[Mapping], *, max_vector_distance: float,
                             area: str | None = None, limit: int = 3) -> list[SearchHit]:
    """Return only current, authorized source segments below a calibrated distance.

    This gate is for retrieval evaluation; it does not prove answer entailment.
    """
    if limit < 1 or not 0 <= max_vector_distance <= 2:
        return []
    resolved = []
    seen = set()
    for candidate in candidates:
        distance = candidate.get("vector_distance")
        if not isinstance(distance, (int, float)) or not 0 <= distance <= max_vector_distance:
            continue
        segment_id = candidate.get("segment_id")
        if type(segment_id) is not int:
            continue
        if segment_id in seen:
            continue
        hit = documents.resolve_search_candidate(
            subject, segment_id=segment_id, document_id=candidate.get("document_id"),
            version=candidate.get("version"), text=candidate.get("body"), area=area)
        if hit is not None:
            seen.add(segment_id)
            resolved.append(hit)
        if len(resolved) >= limit:
            break
    return resolved
