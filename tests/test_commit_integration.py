"""Integration tests for the commit engine against a live Neo4j (docker compose up -d neo4j).

Skipped automatically when Neo4j is unreachable. Each test runs in a wiped database —
these tests own the instance; don't point them at a graph you care about.
"""

from datetime import datetime, timezone

import pytest

from kgi.commit import commit_patch
from kgi.models import (
    AssertEdge,
    CreateNode,
    InvalidateEdge,
    MergeInto,
    NodeRef,
    OpStatus,
    Patch,
    Precondition,
    ReinforceEdge,
    RouteTarget,
)
from kgi.models.patch import RoutedOp
from kgi.stores.neo4j import CanonicalGraph, StagingStore


def _connect() -> CanonicalGraph | None:
    try:
        g = CanonicalGraph()
        g._driver.verify_connectivity()
        return g
    except Exception:
        return None


import os

_graph = _connect()
pytestmark = pytest.mark.skipif(
    _graph is None or os.environ.get("KGI_TEST_ALLOW_WIPE") != "1",
    reason="Neo4j not reachable, or KGI_TEST_ALLOW_WIPE=1 not set — "
           "these tests WIPE the database they point at",
)


@pytest.fixture()
def graph():
    with _graph.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
    _graph.ensure_constraints()
    return _graph


def _routed(op, status=OpStatus.approved) -> RoutedOp:
    return RoutedOp(op=op, route=RouteTarget.review, status=status)


def _patch(ops: list[RoutedOp], patch_id="p1") -> Patch:
    return Patch(
        patch_id=patch_id,
        doc_id="d1",
        extraction_run_id="r1",
        created_at=datetime.now(timezone.utc),
        ops=ops,
    )


def _seed_edge(graph, subj="c_a", obj="c_b", predicate="WORKS_AT", edge_id="e_1"):
    with graph.session() as s:
        s.run(
            "MERGE (a:Canonical {id: $subj}) SET a.name = 'A', a.entity_type = 'Agent' "
            "MERGE (b:Canonical {id: $obj}) SET b.name = 'B', b.entity_type = 'Agent' "
            "MERGE (a)-[r:REL {id: $eid}]->(b) "
            "SET r.predicate = $pred, r.valid_to = null, r.support = 1",
            subj=subj, obj=obj, eid=edge_id, pred=predicate,
        )


def test_create_and_link(graph):
    patch = _patch([
        _routed(CreateNode(op_id="n1", temp_id="t1", entity_type="Agent",
                           properties={"name": "Ada"})),
        _routed(CreateNode(op_id="n2", temp_id="t2", entity_type="Artifact",
                           properties={"name": "Engine"})),
        _routed(AssertEdge(op_id="e1", depends_on=["n1", "n2"],
                           subject=NodeRef(temp_id="t1"), predicate="BUILT",
                           object=NodeRef(temp_id="t2"))),
    ])
    report = commit_patch(graph, patch)
    assert sorted(report.committed) == ["e1", "n1", "n2"]

    with graph.session() as s:
        rec = s.run(
            "MATCH (a:Canonical {name:'Ada'})-[r:REL {predicate:'BUILT'}]->"
            "(b:Canonical {name:'Engine'}) RETURN r.valid_to AS vt"
        ).single()
    assert rec is not None and rec["vt"] is None


def test_idempotent_recommit(graph):
    ops = [
        _routed(CreateNode(op_id="n1", temp_id="t1", entity_type="Agent",
                           properties={"name": "Ada"})),
    ]
    commit_patch(graph, _patch(ops))
    # Simulate crash-and-retry: same patch, statuses back to approved.
    ops[0].status = OpStatus.approved
    report = commit_patch(graph, _patch(ops))
    assert report.committed == ["n1"]  # ledger hit, no second write

    with graph.session() as s:
        count = s.run("MATCH (n:Canonical {name:'Ada'}) RETURN count(n) AS c").single()["c"]
        applied = s.run("MATCH (a:AppliedOp) RETURN count(a) AS c").single()["c"]
    assert count == 1
    assert applied == 1


def test_precondition_failure_requeues(graph):
    # Diffed against an edge that no longer exists -> rebase check must hold the op.
    patch = _patch([
        _routed(InvalidateEdge(
            op_id="i1", canonical_edge_id="e_gone", valid_to="2026-07-08",
            reason="superseded",
            preconditions=[Precondition(kind="edge_exists", subject="e_gone")],
        )),
    ])
    report = commit_patch(graph, patch)
    assert report.requeued == ["i1"]
    assert patch.ops[0].status == OpStatus.requeued


def test_blocked_when_dependency_requeued(graph):
    patch = _patch([
        _routed(CreateNode(
            op_id="n1", temp_id="t1", entity_type="Agent",
            preconditions=[Precondition(kind="node_exists", subject="c_missing")],
        )),
        _routed(AssertEdge(op_id="e1", depends_on=["n1"],
                           subject=NodeRef(temp_id="t1"), predicate="KNOWS",
                           object=NodeRef(temp_id="t1"))),
    ])
    report = commit_patch(graph, patch)
    assert report.requeued == ["n1"]
    assert report.blocked == ["e1"]


def test_merge_into_is_reversible(graph):
    _seed_edge(graph)
    patch = _patch([
        _routed(MergeInto(op_id="m1", temp_id="t1", canonical_id="c_a",
                          match_score=0.97, match_method="embedding")),
        _routed(AssertEdge(op_id="e1", depends_on=["m1"],
                           subject=NodeRef(temp_id="t1"), predicate="LOCATED_IN",
                           object=NodeRef(canonical_id="c_b"))),
    ])
    report = commit_patch(graph, patch)
    assert sorted(report.committed) == ["e1", "m1"]

    with graph.session() as s:
        # SAME_AS provenance survives; the new edge landed on the canonical node.
        alias = s.run(
            "MATCH (a:Alias)-[:SAME_AS]->(c:Canonical {id:'c_a'}) RETURN a.match_method AS m"
        ).single()
        edge = s.run(
            "MATCH (:Canonical {id:'c_a'})-[r:REL {predicate:'LOCATED_IN'}]->"
            "(:Canonical {id:'c_b'}) RETURN r.id AS id"
        ).single()
    assert alias is not None and alias["m"] == "embedding"
    assert edge is not None


def test_invalidate_and_reinforce(graph):
    _seed_edge(graph, edge_id="e_1")
    patch = _patch([
        _routed(InvalidateEdge(
            op_id="i1", canonical_edge_id="e_1", valid_to="2026-07-08",
            reason="superseded",
            preconditions=[Precondition(kind="edge_exists", subject="e_1")],
        )),
    ])
    assert commit_patch(graph, patch).committed == ["i1"]

    _seed_edge(graph, edge_id="e_2", predicate="MANAGES")
    patch2 = _patch([_routed(ReinforceEdge(op_id="r1", canonical_edge_id="e_2"))],
                    patch_id="p2")
    assert commit_patch(graph, patch2).committed == ["r1"]

    with graph.session() as s:
        closed = s.run("MATCH ()-[r:REL {id:'e_1'}]->() RETURN r.valid_to AS vt").single()
        support = s.run("MATCH ()-[r:REL {id:'e_2'}]->() RETURN r.support AS s").single()
    assert closed["vt"] == "2026-07-08"
    assert support["s"] == 2


def test_staging_roundtrip(graph):
    staging = StagingStore()
    patch = _patch([
        _routed(CreateNode(op_id="n1", temp_id="t1", entity_type="Agent"),
                status=OpStatus.pending),
    ])
    staging.save_patch(patch)
    assert [p.patch_id for p in staging.pending_patches()] == ["p1"]
    loaded = staging.load_patch("p1")
    assert loaded.ops[0].op.op_id == "n1"
    staging.set_status("p1", "committed")
    assert staging.pending_patches() == []
    staging.close()
