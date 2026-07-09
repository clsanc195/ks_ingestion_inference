"""The staging patch model — the unit of everything downstream of resolution.

A Patch is a proposed mutation of the canonical graph, produced by resolution/
correlation (L5-L6), scored (L7), routed (L8), reviewed (L9), and applied (L10).

Design decisions (see architecture doc §3.2, §7):

- Ops form a DAG via `depends_on`. An op is only committable when its dependency
  closure is approved: an AssertEdge referencing a temp node depends on that node's
  CreateNode (or MergeInto). Routing/review happen per-op, but commit applies ops
  in dependency order and holds back ops whose dependencies were rejected.
- Every op carries `preconditions` capturing the canonical state it was diffed
  against. Rebase-at-commit (§7): if a precondition no longer holds, the op is not
  applied blind — it is re-diffed and re-queued.
- MergeInto never destroys the source node: it keeps a SAME_AS edge so merges are
  reversible (§7 reversibility decision).
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class NodeRef(BaseModel):
    """Reference to a node: exactly one of temp_id (staged) or canonical_id must be set."""

    temp_id: str | None = None
    canonical_id: str | None = None

    def is_staged(self) -> bool:
        return self.temp_id is not None


class Precondition(BaseModel):
    """An assumption about canonical state, checked again at commit time (rebase check).

    kind examples: node_exists | edge_exists | edge_absent | prop_equals | props_hash
    """

    kind: str
    subject: str  # canonical id (node or edge) the check applies to
    expected: dict = Field(default_factory=dict)


class _OpBase(BaseModel):
    op_id: str
    depends_on: list[str] = Field(default_factory=list)  # op_ids within the same patch
    preconditions: list[Precondition] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)  # filled by L7
    rationale: str = ""  # human-readable why (shown in review UI)


class CreateNode(_OpBase):
    op: Literal["create_node"] = "create_node"
    temp_id: str
    entity_type: str
    properties: dict = Field(default_factory=dict)


class UpdateNodeProps(_OpBase):
    """Extend/refresh an existing canonical node (correlation outcome: 'extends')."""

    op: Literal["update_node_props"] = "update_node_props"
    canonical_id: str
    properties: dict


class MergeInto(_OpBase):
    """Staged entity resolved to an existing canonical node (correlation: 'is').

    Reversible: the staged node's identity is preserved via a SAME_AS provenance
    edge; properties/edges consolidate onto the canonical node.
    """

    op: Literal["merge_into"] = "merge_into"
    temp_id: str
    canonical_id: str
    match_score: float = Field(ge=0.0, le=1.0)
    match_method: str  # rules | embedding | llm  (which cascade tier decided, L5)


class SplitNode(_OpBase):
    """Un-merge an over-merged canonical node (correlation: 'refines granularity')."""

    op: Literal["split_node"] = "split_node"
    canonical_id: str
    into: list[dict]  # descriptions of the resulting nodes


class AssertEdge(_OpBase):
    op: Literal["assert_edge"] = "assert_edge"
    subject: NodeRef
    predicate: str
    object: NodeRef
    properties: dict = Field(default_factory=dict)
    valid_from: str | None = None  # bi-temporal valid-time (L6)


class InvalidateEdge(_OpBase):
    """Soft-close a canonical edge (contradiction, temporal supersession). Never deletes."""

    op: Literal["invalidate_edge"] = "invalidate_edge"
    canonical_edge_id: str
    valid_to: str
    reason: str  # superseded | contradicted | reviewer_rejected


class ReinforceEdge(_OpBase):
    """New source asserts an existing fact (correlation: 'reinforces')."""

    op: Literal["reinforce_edge"] = "reinforce_edge"
    canonical_edge_id: str


PatchOp = Annotated[
    Union[CreateNode, UpdateNodeProps, MergeInto, SplitNode, AssertEdge, InvalidateEdge, ReinforceEdge],
    Field(discriminator="op"),
]


class RouteTarget(str, Enum):
    auto_approve = "auto_approve"
    review = "review"
    auto_reject = "auto_reject"


class OpStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    committed = "committed"
    requeued = "requeued"  # rebase check failed; back through resolution
    blocked = "blocked"  # a dependency was rejected


class RoutedOp(BaseModel):
    op: PatchOp
    route: RouteTarget
    status: OpStatus = OpStatus.pending


class Patch(BaseModel):
    """A proposed change-set against canonical, with full lineage."""

    patch_id: str
    doc_id: str
    extraction_run_id: str
    created_at: datetime
    ops: list[RoutedOp] = Field(default_factory=list)

    def committable_ops(self) -> list[RoutedOp]:
        """Approved ops whose full dependency closure is also approved/committed."""
        by_id = {r.op.op_id: r for r in self.ops}
        ok_status = {OpStatus.approved, OpStatus.committed}

        def closure_ok(r: RoutedOp) -> bool:
            return all(
                d in by_id and by_id[d].status in ok_status and closure_ok(by_id[d])
                for d in r.op.depends_on
            )

        return [r for r in self.ops if r.status == OpStatus.approved and closure_ok(r)]
