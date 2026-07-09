"""Routing policy (L8): static thresholds v1, active-learning selection v2.

Force-review overrides (always human, regardless of score):
- ops introducing a NEW schema type (§7 open-domain decision)
- semantic-conflict op pairs (§7 tiered contradiction policy)
- SplitNode and MergeInto of high-degree canonical nodes (over-merge blast radius)
"""

from kgi.config import settings
from kgi.models import MergeInto, PatchOp, RouteTarget, SplitNode

HIGH_DEGREE_REVIEW_THRESHOLD = 50  # canonical node degree above which merges always review


def route_op(
    op: PatchOp,
    *,
    introduces_new_type: bool = False,
    is_semantic_conflict: bool = False,
    canonical_degree: int = 0,
) -> RouteTarget:
    if introduces_new_type or is_semantic_conflict:
        return RouteTarget.review
    if isinstance(op, SplitNode):
        return RouteTarget.review
    if isinstance(op, MergeInto) and canonical_degree >= HIGH_DEGREE_REVIEW_THRESHOLD:
        return RouteTarget.review

    cfg = settings()
    if op.confidence >= cfg.route_auto_approve:
        return RouteTarget.auto_approve
    if op.confidence < cfg.route_auto_reject:
        return RouteTarget.auto_reject
    return RouteTarget.review
