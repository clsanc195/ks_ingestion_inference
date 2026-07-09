"""Conflict & temporal reconciliation (L6) — where correlation outcomes become patch ops.

Tiered contradiction policy (§7):
- temporal contradiction (value changed over time) -> auto: InvalidateEdge(valid_to)
  + AssertEdge(new), both ops still subject to routing/confidence but eligible for
  auto-approve.
- semantic conflict (same validity window, incompatible claims) -> AssertEdge +
  InvalidateEdge pair FORCE-ROUTED to human review regardless of confidence.
"""

from enum import Enum

from kgi.models import CandidateRelation, PatchOp


class Correlation(str, Enum):
    new = "new"  # nothing related in canonical
    reinforces = "reinforces"  # same fact already present
    extends = "extends"  # adds to a known entity
    contradicts_temporal = "contradicts_temporal"
    contradicts_semantic = "contradicts_semantic"


def correlate_relation(
    relation: CandidateRelation,
    subject_canonical_id: str | None,
    object_canonical_id: str | None,
) -> tuple[Correlation, list[PatchOp]]:
    """Compare a staged relation against canonical edges between the resolved endpoints
    and emit the patch ops for the correlation outcome (§3.2 table).

    v1 stub. Implementation queries canonical for edges (subject)-[predicate]->(*),
    classifies via valid-time overlap + value comparison, and builds ops with
    preconditions snapshotting the edges it diffed against (rebase-at-commit, §7).
    """
    raise NotImplementedError
