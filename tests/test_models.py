"""Smoke tests: the patch model's dependency-closure logic, the core of commit safety."""

from datetime import datetime, timezone

from kgi.models import (
    AssertEdge,
    CreateNode,
    NodeRef,
    OpStatus,
    Patch,
    RouteTarget,
)
from kgi.models.patch import RoutedOp


def _patch(ops: list[RoutedOp]) -> Patch:
    return Patch(
        patch_id="p1",
        doc_id="d1",
        extraction_run_id="r1",
        created_at=datetime.now(timezone.utc),
        ops=ops,
    )


def _node(op_id: str, status: OpStatus) -> RoutedOp:
    return RoutedOp(
        op=CreateNode(op_id=op_id, temp_id=f"t_{op_id}", entity_type="Agent"),
        route=RouteTarget.review,
        status=status,
    )


def _edge(op_id: str, depends_on: list[str], status: OpStatus) -> RoutedOp:
    return RoutedOp(
        op=AssertEdge(
            op_id=op_id,
            depends_on=depends_on,
            subject=NodeRef(temp_id="t_n1"),
            predicate="WORKS_AT",
            object=NodeRef(canonical_id="c_42"),
        ),
        route=RouteTarget.review,
        status=status,
    )


def test_edge_committable_when_dependency_approved():
    p = _patch([_node("n1", OpStatus.approved), _edge("e1", ["n1"], OpStatus.approved)])
    assert {r.op.op_id for r in p.committable_ops()} == {"n1", "e1"}


def test_edge_held_back_when_dependency_rejected():
    p = _patch([_node("n1", OpStatus.rejected), _edge("e1", ["n1"], OpStatus.approved)])
    assert {r.op.op_id for r in p.committable_ops()} == set()


def test_committed_dependency_satisfies_closure():
    p = _patch([_node("n1", OpStatus.committed), _edge("e1", ["n1"], OpStatus.approved)])
    assert {r.op.op_id for r in p.committable_ops()} == {"e1"}
