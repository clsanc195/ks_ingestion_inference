"""Commit / merge engine (L10): the ONLY code path that mutates canonical (core invariant).

- Patch applier, not an inserter: ops cover merge/update/invalidate/reinforce (§3.2).
- Idempotent at op level: every applied op writes an (:AppliedOp {op_id}) ledger node
  in the same transaction as the fact; a re-run sees the ledger entry and skips.
  Canonical ids for created nodes/edges are minted deterministically from
  (patch_id, op_id), so a crash-and-retry converges on the same graph.
- Rebase-at-commit (§7): preconditions are re-checked inside the op's transaction;
  mismatch -> op marked `requeued` and NOT applied. The caller re-diffs those ops.
- Reversible merges (§7): MergeInto writes an (:Alias)-[:SAME_AS]->(:Canonical) node
  instead of destroying the staged identity; new properties only fill gaps, never
  overwrite reviewer-visible canonical values.
- Dependency order: ops apply topologically; an op whose dependency was rejected or
  requeued is marked `blocked` and skipped (Patch.committable_ops is the gate).
- SplitNode is deliberately unsupported in v1 (always human-routed; rare enough that
  the reviewer performs it as a manual patch).
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from graphlib import TopologicalSorter

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
    SplitNode,
    UpdateNodeProps,
)
from kgi.models.patch import RoutedOp
from kgi.stores.neo4j import CanonicalGraph


@dataclass
class CommitReport:
    committed: list[str] = field(default_factory=list)  # op_ids applied (or already applied)
    requeued: list[str] = field(default_factory=list)  # failed rebase check
    blocked: list[str] = field(default_factory=list)  # dependency rejected/requeued
    skipped: list[str] = field(default_factory=list)  # not approved (pending/rejected)


def _mint(patch_id: str, op_id: str, prefix: str) -> str:
    return f"{prefix}_{hashlib.sha1(f'{patch_id}:{op_id}'.encode()).hexdigest()[:16]}"


def _check_precondition(tx, pre: Precondition) -> bool:
    if pre.kind == "node_exists":
        return tx.run(
            "MATCH (n:Canonical {id: $id}) RETURN n.id", id=pre.subject
        ).single() is not None
    if pre.kind == "edge_exists":
        return tx.run(
            "MATCH ()-[r:REL {id: $id}]->() WHERE r.valid_to IS NULL RETURN r.id",
            id=pre.subject,
        ).single() is not None
    if pre.kind == "edge_absent":
        return tx.run(
            "MATCH (a:Canonical {id: $subj})-[r:REL {predicate: $pred}]->"
            "(b:Canonical {id: $obj}) WHERE r.valid_to IS NULL RETURN r.id",
            subj=pre.subject, pred=pre.expected["predicate"], obj=pre.expected["object_id"],
        ).single() is None
    if pre.kind == "prop_equals":
        rec = tx.run(
            "MATCH (n:Canonical {id: $id}) RETURN n[$prop] AS v",
            id=pre.subject, prop=pre.expected["prop"],
        ).single()
        return rec is not None and rec["v"] == pre.expected["value"]
    raise ValueError(f"unknown precondition kind {pre.kind!r}")


def _write_ledger(tx, patch: Patch, op_id: str, wrote_node: str | None = None,
                  wrote_edge: str | None = None) -> None:
    tx.run(
        "CREATE (a:AppliedOp {op_id: $op_id, patch_id: $patch_id, doc_id: $doc_id, "
        "run_id: $run_id, at: $at, edge_id: $edge_id}) "
        "WITH a MATCH (n:Canonical|Alias {id: $node_id}) CREATE (a)-[:WROTE]->(n)",
        op_id=op_id, patch_id=patch.patch_id, doc_id=patch.doc_id,
        run_id=patch.extraction_run_id, at=datetime.now(timezone.utc).isoformat(),
        edge_id=wrote_edge, node_id=wrote_node,
    )


class _OpApplier:
    """Applies one op inside one transaction. Returns the minted id (if any)."""

    def __init__(self, patch: Patch, id_map: dict[str, str]):
        self.patch = patch
        self.id_map = id_map  # temp_id -> canonical id (from CreateNode/MergeInto ops)

    def _resolve(self, ref: NodeRef) -> str:
        if ref.canonical_id:
            return ref.canonical_id
        if ref.temp_id and ref.temp_id in self.id_map:
            return self.id_map[ref.temp_id]
        raise KeyError(f"unresolvable node ref {ref!r} — missing dependency op?")

    def apply(self, tx, op) -> None:
        if isinstance(op, CreateNode):
            cid = _mint(self.patch.patch_id, op.op_id, "can")
            self.id_map[op.temp_id] = cid
            tx.run(
                "MERGE (n:Canonical {id: $id}) "
                "SET n += $props, n.entity_type = $type, n.confidence = $conf",
                id=cid, props=op.properties, type=op.entity_type, conf=op.confidence,
            )
            _write_ledger(tx, self.patch, op.op_id, wrote_node=cid)
        elif isinstance(op, UpdateNodeProps):
            tx.run(
                "MATCH (n:Canonical {id: $id}) SET n += $props",
                id=op.canonical_id, props=op.properties,
            )
            _write_ledger(tx, self.patch, op.op_id, wrote_node=op.canonical_id)
        elif isinstance(op, MergeInto):
            self.id_map[op.temp_id] = op.canonical_id
            alias_id = _mint(self.patch.patch_id, op.op_id, "alias")
            # Fill-gaps-only property merge: canonical values win over staged ones.
            tx.run(
                "MATCH (c:Canonical {id: $cid}) "
                "MERGE (a:Alias {id: $aid}) "
                "SET a.match_score = $score, a.match_method = $method "
                "MERGE (a)-[:SAME_AS]->(c)",
                cid=op.canonical_id, aid=alias_id,
                score=op.match_score, method=op.match_method,
            )
            _write_ledger(tx, self.patch, op.op_id, wrote_node=alias_id)
        elif isinstance(op, AssertEdge):
            eid = _mint(self.patch.patch_id, op.op_id, "edge")
            tx.run(
                "MATCH (a:Canonical {id: $subj}), (b:Canonical {id: $obj}) "
                "MERGE (a)-[r:REL {id: $eid}]->(b) "
                "SET r.predicate = $pred, r.valid_from = $vf, r.valid_to = null, "
                "    r.support = coalesce(r.support, 1), r.confidence = $conf, "
                "    r += $props",
                subj=self._resolve(op.subject), obj=self._resolve(op.object),
                eid=eid, pred=op.predicate, vf=op.valid_from,
                conf=op.confidence, props=op.properties,
            )
            _write_ledger(tx, self.patch, op.op_id, wrote_edge=eid)
        elif isinstance(op, InvalidateEdge):
            tx.run(
                "MATCH ()-[r:REL {id: $id}]->() "
                "SET r.valid_to = $vt, r.invalidation_reason = $reason",
                id=op.canonical_edge_id, vt=op.valid_to, reason=op.reason,
            )
            _write_ledger(tx, self.patch, op.op_id, wrote_edge=op.canonical_edge_id)
        elif isinstance(op, ReinforceEdge):
            # Evidence accumulates: the new source's quote joins r.quotes (list
            # seeded from the legacy single r.quote), skipping exact repeats.
            tx.run(
                "MATCH ()-[r:REL {id: $id}]->() "
                "WITH r, coalesce(r.quotes, CASE WHEN r.quote IS NULL OR r.quote = '' "
                "     THEN [] ELSE [r.quote] END) AS qs "
                "SET r.support = coalesce(r.support, 1) + 1, "
                "    r.quotes = CASE WHEN $q IS NULL OR $q = '' OR $q IN qs "
                "               THEN qs ELSE qs + $q END",
                id=op.canonical_edge_id, q=op.quote,
            )
            _write_ledger(tx, self.patch, op.op_id, wrote_edge=op.canonical_edge_id)
        elif isinstance(op, SplitNode):
            raise NotImplementedError("SplitNode commit is not supported in v1")
        else:
            raise TypeError(f"unknown op type {type(op).__name__}")


def _topo_order(ops: list[RoutedOp]) -> list[RoutedOp]:
    by_id = {r.op.op_id: r for r in ops}
    ts = TopologicalSorter({r.op.op_id: list(r.op.depends_on) for r in ops})
    return [by_id[op_id] for op_id in ts.static_order() if op_id in by_id]


def commit_patch(graph: CanonicalGraph, patch: Patch) -> CommitReport:
    """Apply the patch's approved ops in dependency order, one transaction per op
    (fact + ledger/provenance together). Mutates op statuses on the patch in place;
    the caller persists the patch back to staging."""
    report = CommitReport()
    applier = _OpApplier(patch, id_map={})
    failed_deps: set[str] = set()

    with graph.session() as session:
        for routed in _topo_order(patch.ops):
            op = routed.op

            if routed.status not in (OpStatus.approved, OpStatus.committed):
                report.skipped.append(op.op_id)
                failed_deps.add(op.op_id)
                continue
            if any(d in failed_deps for d in op.depends_on):
                routed.status = OpStatus.blocked
                report.blocked.append(op.op_id)
                failed_deps.add(op.op_id)
                continue

            def _tx(tx, routed=routed, op=op):
                already = tx.run(
                    "MATCH (a:AppliedOp {op_id: $id}) RETURN a.op_id", id=op.op_id
                ).single()
                if already:
                    # Re-run after crash: re-register minted ids so later ops resolve.
                    if isinstance(op, CreateNode):
                        applier.id_map[op.temp_id] = _mint(patch.patch_id, op.op_id, "can")
                    elif isinstance(op, MergeInto):
                        applier.id_map[op.temp_id] = op.canonical_id
                    return "committed"
                if not all(_check_precondition(tx, p) for p in op.preconditions):
                    return "requeued"
                applier.apply(tx, op)
                return "committed"

            outcome = session.execute_write(_tx)
            if outcome == "committed":
                routed.status = OpStatus.committed
                report.committed.append(op.op_id)
            else:
                routed.status = OpStatus.requeued
                report.requeued.append(op.op_id)
                failed_deps.add(op.op_id)

    return report
