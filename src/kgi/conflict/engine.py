"""Conflict & temporal reconciliation (L6) — where correlation outcomes become patch ops.

For a staged relation whose endpoints both resolved to canonical nodes:

  exact same edge exists            -> reinforces        (ReinforceEdge)
  no same-predicate edge            -> new               (AssertEdge)
  same predicate, different object  -> adjudicate each existing edge:
      compatible             both can hold (multi-valued predicate)  -> extends
      temporal_supersession  value changed over time (§7: auto tier) -> InvalidateEdge
                             (valid_to = new valid_from) + AssertEdge
      semantic_conflict      incompatible same-time claims (§7: always
                             human) -> AssertEdge + InvalidateEdge both
                             FORCE-ROUTED to review; the reviewer keeps
                             one, both (contested), or neither

Adjudication is an LLM call with a conservative prompt: absent clear evidence of
change over time it must answer semantic_conflict, because a wrong auto-invalidation
silently rewrites history while an escalation merely costs one review.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from kgi.models import (
    AssertEdge,
    CandidateRelation,
    InvalidateEdge,
    NodeRef,
    Precondition,
    ReinforceEdge,
)
from kgi.stores.neo4j import CanonicalGraph


class Correlation(str, Enum):
    new = "new"  # nothing related in canonical
    reinforces = "reinforces"  # same fact already present
    extends = "extends"  # coexists with a same-predicate fact (compatible)
    contradicts_temporal = "contradicts_temporal"
    contradicts_semantic = "contradicts_semantic"


@dataclass
class CorrelatedOp:
    op: object  # a PatchOp
    resolution: float  # resolution/adjudication confidence signal for L7
    semantic: bool = False  # force-route to review (L8, §7)


class _ConflictJudgment(BaseModel):
    verdict: Literal["compatible", "temporal_supersession", "semantic_conflict"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


_ADJUDICATOR_SYSTEM = """You adjudicate potential contradictions in a knowledge graph.
An existing fact and a new fact share the same predicate and one endpoint, but differ
on the other endpoint. Classify the pair:

- compatible: both can be true at the same time (multi-valued — a company has many
  divisions; a company can be founded by several co-founders; a person can serve on
  several boards).
- temporal_supersession: the new fact replaces the old one over time — the evidence
  clearly indicates a change (a move, a successor taking a role, an acquisition,
  explicit dates or words like "replaced" / "until").
- semantic_conflict: the claims cannot both be true and there is no clear evidence
  of change over time.

Roles like CEO or chairman of one organization are typically held by one person at a
time — a new holder with evidence of succession is temporal_supersession.

Be conservative: if the evidence does not clearly indicate change over time, answer
semantic_conflict rather than temporal_supersession. A wrong supersession silently
rewrites history; an escalation only costs one human review."""


def _adjudicate(
    old_fact: str,
    old_edge: dict,
    new_fact: str,
    relation: CandidateRelation,
) -> _ConflictJudgment:
    from kgi.llm import structured_call

    evidence = "; ".join(s.quote for s in relation.evidence[:3])
    return structured_call(
        "conflict-adjudicator",
        _ConflictJudgment,
        system=_ADJUDICATOR_SYSTEM,
        user=(
            f"EXISTING fact: {old_fact}"
            f"  valid_from={old_edge.get('valid_from')!r}"
            f"  supported by {old_edge.get('support', 1)} source(s)\n"
            f"NEW fact: {new_fact}  valid_from={relation.valid_from!r}\n"
            f"NEW fact evidence: {evidence!r}"
        ),
    )


def _op_id() -> str:
    return f"op_{uuid.uuid4().hex[:12]}"


def correlate_relation(
    graph: CanonicalGraph,
    relation: CandidateRelation,
    subject_ref: NodeRef,
    subject_name: str,
    object_ref: NodeRef,
    object_name: str,
    depends_on: list[str] | None = None,
) -> tuple[Correlation, list[CorrelatedOp]]:
    """Compare a staged relation against canonical edges sharing either endpoint and
    emit the patch ops for the correlation outcome (§3.2 table).

    Runs whenever EITHER endpoint is canonical. Subject-side conflicts: same subject,
    same predicate, different object (a company moved HQ). Object-side conflicts:
    same object, same predicate, different subject — the succession shape:
    (Körner)-[ceo_of]->(CS) arriving while (Gottstein)-[ceo_of]->(CS) is current,
    where the new subject may be a node this patch is only just creating.
    """
    both_canonical = subject_ref.canonical_id and object_ref.canonical_id
    if both_canonical:
        exact = graph.edges_between(
            subject_ref.canonical_id, object_ref.canonical_id, relation.predicate
        )
        if exact:
            return Correlation.reinforces, [
                CorrelatedOp(
                    ReinforceEdge(
                        op_id=_op_id(),
                        canonical_edge_id=exact[0]["id"],
                        quote=(relation.evidence[0].quote[:300]
                               if relation.evidence else None),
                        preconditions=[
                            Precondition(kind="edge_exists", subject=exact[0]["id"])
                        ],
                        rationale=f"another source asserts {relation.predicate}",
                    ),
                    resolution=1.0,
                )
            ]

    preconditions = []
    if both_canonical:
        preconditions.append(
            Precondition(
                kind="edge_absent",
                subject=subject_ref.canonical_id,
                expected={"predicate": relation.predicate,
                          "object_id": object_ref.canonical_id},
            )
        )
    assert_op = AssertEdge(
        op_id=_op_id(),
        depends_on=depends_on or [],
        subject=subject_ref,
        predicate=relation.predicate,
        object=object_ref,
        properties=relation.properties,
        valid_from=relation.valid_from,
        preconditions=preconditions,
        rationale=f"asserts {relation.predicate}",
    )
    new_fact = f"({subject_name}) -[{relation.predicate}]-> ({object_name})"

    # Existing current edges that share one endpoint + predicate but differ on the
    # other endpoint: each is a potential contradiction to adjudicate.
    colliding: list[tuple[str, dict]] = []  # (old fact rendering, edge dict)
    if subject_ref.canonical_id:
        colliding += [
            (f"({subject_name}) -[{relation.predicate}]-> ({e['object_name']})", e)
            for e in graph.edges_from(subject_ref.canonical_id, relation.predicate)
            if e["object_id"] != object_ref.canonical_id
        ]
    if object_ref.canonical_id:
        colliding += [
            (f"({e['subject_name']}) -[{relation.predicate}]-> ({object_name})", e)
            for e in graph.edges_to(object_ref.canonical_id, relation.predicate)
            if e["subject_id"] != subject_ref.canonical_id
        ]
    if not colliding:
        return Correlation.new, [CorrelatedOp(assert_op, resolution=0.5)]

    outcome = Correlation.extends
    ops: list[CorrelatedOp] = [CorrelatedOp(assert_op, resolution=0.5)]
    for old_fact, old in colliding:
        judgment = _adjudicate(old_fact, old, new_fact, relation)
        if judgment.verdict == "temporal_supersession":
            outcome = (Correlation.contradicts_temporal
                       if outcome != Correlation.contradicts_semantic else outcome)
            ops.append(
                CorrelatedOp(
                    InvalidateEdge(
                        op_id=_op_id(),
                        canonical_edge_id=old["id"],
                        valid_to=relation.valid_from or date.today().isoformat(),
                        reason="superseded",
                        preconditions=[Precondition(kind="edge_exists", subject=old["id"])],
                        rationale=(
                            f"superseded: {old_fact} replaced by {new_fact}. "
                            f"{judgment.reasoning}"
                        ),
                    ),
                    resolution=judgment.confidence,
                )
            )
        elif judgment.verdict == "semantic_conflict":
            outcome = Correlation.contradicts_semantic
            ops[0].semantic = True  # the new assertion itself needs human eyes
            ops[0].op.rationale = f"CONFLICTS with {old_fact}: {judgment.reasoning}"
            ops.append(
                CorrelatedOp(
                    InvalidateEdge(
                        op_id=_op_id(),
                        canonical_edge_id=old["id"],
                        valid_to=relation.valid_from or date.today().isoformat(),
                        reason="contradicted",
                        preconditions=[Precondition(kind="edge_exists", subject=old["id"])],
                        rationale=(
                            f"contested by new claim {new_fact}; accept to retire "
                            f"the old fact, reject to keep both as contested"
                        ),
                    ),
                    resolution=judgment.confidence,
                    semantic=True,
                )
            )
    return outcome, ops
