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
An existing fact and a new fact share the same subject and predicate but assert
different objects. Classify the pair:

- compatible: both can be true at the same time (multi-valued predicate — a company
  produces many products, a person founded several companies).
- temporal_supersession: the new fact replaces the old one over time — the evidence
  clearly indicates a change (a move, a role change, an acquisition, explicit dates).
- semantic_conflict: the claims cannot both be true and there is no clear evidence
  of change over time.

Be conservative: if the evidence does not clearly indicate change over time, answer
semantic_conflict rather than temporal_supersession. A wrong supersession silently
rewrites history; an escalation only costs one human review."""


def _adjudicate(
    subject_name: str,
    predicate: str,
    old_object_name: str,
    old_edge: dict,
    new_object_name: str,
    relation: CandidateRelation,
) -> _ConflictJudgment:
    import anthropic
    import instructor
    from dotenv import load_dotenv

    from kgi.config import settings

    load_dotenv()
    client = instructor.from_anthropic(anthropic.Anthropic())
    evidence = "; ".join(s.quote for s in relation.evidence[:3])
    return client.chat.completions.create(
        model=settings().extraction_model,
        max_tokens=1024,
        response_model=_ConflictJudgment,
        messages=[
            {"role": "system", "content": _ADJUDICATOR_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"EXISTING fact: ({subject_name}) -[{predicate}]-> ({old_object_name})"
                    f"  valid_from={old_edge.get('valid_from')!r}"
                    f"  supported by {old_edge.get('support', 1)} source(s)\n"
                    f"NEW fact: ({subject_name}) -[{predicate}]-> ({new_object_name})"
                    f"  valid_from={relation.valid_from!r}\n"
                    f"NEW fact evidence: {evidence!r}"
                ),
            },
        ],
    )


def _op_id() -> str:
    return f"op_{uuid.uuid4().hex[:12]}"


def correlate_relation(
    graph: CanonicalGraph,
    relation: CandidateRelation,
    subject_id: str,
    object_ref: NodeRef,
    object_name: str,
    depends_on: list[str] | None = None,
) -> tuple[Correlation, list[CorrelatedOp]]:
    """Compare a staged relation against canonical edges of the resolved subject and
    emit the patch ops for the correlation outcome (§3.2 table).

    Runs whenever the SUBJECT is canonical — the object may be brand-new this patch
    (the typical contradiction: a move points at a city the graph has never seen).
    """
    if object_ref.canonical_id:
        exact = graph.edges_between(subject_id, object_ref.canonical_id, relation.predicate)
        if exact:
            return Correlation.reinforces, [
                CorrelatedOp(
                    ReinforceEdge(
                        op_id=_op_id(),
                        canonical_edge_id=exact[0]["id"],
                        preconditions=[
                            Precondition(kind="edge_exists", subject=exact[0]["id"])
                        ],
                        rationale=f"another source asserts {relation.predicate}",
                    ),
                    resolution=1.0,
                )
            ]

    subject_name = (graph.get_node(subject_id) or {}).get("name", subject_id)
    preconditions = []
    if object_ref.canonical_id:
        preconditions.append(
            Precondition(
                kind="edge_absent",
                subject=subject_id,
                expected={"predicate": relation.predicate,
                          "object_id": object_ref.canonical_id},
            )
        )
    assert_op = AssertEdge(
        op_id=_op_id(),
        depends_on=depends_on or [],
        subject=NodeRef(canonical_id=subject_id),
        predicate=relation.predicate,
        object=object_ref,
        properties=relation.properties,
        valid_from=relation.valid_from,
        preconditions=preconditions,
        rationale=f"asserts {relation.predicate}",
    )

    others = [
        e for e in graph.edges_from(subject_id, relation.predicate)
        if e["object_id"] != object_ref.canonical_id
    ]
    if not others:
        return Correlation.new, [CorrelatedOp(assert_op, resolution=0.5)]

    outcome = Correlation.extends
    ops: list[CorrelatedOp] = [CorrelatedOp(assert_op, resolution=0.5)]
    for old in others:
        old_object_name = (graph.get_node(old["object_id"]) or {}).get("name", "?")
        judgment = _adjudicate(
            subject_name, relation.predicate, old_object_name, old, object_name, relation
        )
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
                            f"superseded: ({subject_name}) -[{relation.predicate}]-> "
                            f"({old_object_name}) replaced by ({object_name}). "
                            f"{judgment.reasoning}"
                        ),
                    ),
                    resolution=judgment.confidence,
                )
            )
        elif judgment.verdict == "semantic_conflict":
            outcome = Correlation.contradicts_semantic
            ops[0].semantic = True  # the new assertion itself needs human eyes
            ops[0].op.rationale = (
                f"CONFLICTS with ({subject_name}) -[{relation.predicate}]-> "
                f"({old_object_name}): {judgment.reasoning}"
            )
            ops.append(
                CorrelatedOp(
                    InvalidateEdge(
                        op_id=_op_id(),
                        canonical_edge_id=old["id"],
                        valid_to=relation.valid_from or date.today().isoformat(),
                        reason="contradicted",
                        preconditions=[Precondition(kind="edge_exists", subject=old["id"])],
                        rationale=(
                            f"contested by new claim ({object_name}); accept to retire "
                            f"the old fact, reject to keep both as contested"
                        ),
                    ),
                    resolution=judgment.confidence,
                    semantic=True,
                )
            )
    return outcome, ops
