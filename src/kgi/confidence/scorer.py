"""Confidence scoring (L7): weighted linear blend v1; learned classifier v2 once
reviewer decisions accumulate. Final score is stored on the op and, after commit,
as a queryable graph attribute."""

from kgi.models import PatchOp

# v1 weights — guesses to be calibrated against early reviewer decisions (L15).
WEIGHTS = {
    "extraction": 0.35,  # extractor self-reported / self-consistency
    "support": 0.25,  # source support count (SCICERO)
    "resolution": 0.25,  # match score from the cascade tier that decided
    "validation": 0.15,  # ontology / SHACL pass
}


def score_op(
    op: PatchOp,
    extraction_confidence: float,
    support_count: int,
    resolution_score: float,
    validation_passed: bool,
) -> float:
    support = min(support_count / 3.0, 1.0)  # saturate at 3 independent sources
    return round(
        WEIGHTS["extraction"] * extraction_confidence
        + WEIGHTS["support"] * support
        + WEIGHTS["resolution"] * resolution_score
        + WEIGHTS["validation"] * (1.0 if validation_passed else 0.0),
        4,
    )
