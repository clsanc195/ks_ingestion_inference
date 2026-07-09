"""Entity resolution cascade (L5): rules -> embedding cosine -> LLM pairwise.

Open-domain (§7): the rules tier is exact-name only, so the embedding and LLM tiers
carry most of the load. Thresholds were calibrated empirically on bge-small-en-v1.5:
true aliases ("Acme Corp"/"Acme Corporation") score ~0.99, but *distinct* siblings
("GripperOne"/"GripperTwo") score ~0.87 — auto-accepting at 0.85 would over-merge
(§6.4), hence the wide ambiguous band handed to the LLM judge.

Predicate paraphrase is handled the same way at a milder threshold: "is chief
executive officer of" ≈ "is CEO of" (0.87) normalizes to the known predicate, while
unrelated predicates sit ≤0.7.
"""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from kgi.models import CandidateEntity
from kgi.stores.neo4j import CanonicalGraph
from kgi.stores.qdrant import EntityVectors, PredicateVectors

EMBEDDING_ACCEPT = 0.93  # >= : same entity, no LLM needed
EMBEDDING_REJECT = 0.70  # <  : new entity, no LLM needed
PREDICATE_NORMALIZE = 0.85  # >= : reuse the existing predicate string


@dataclass
class MatchResult:
    canonical_id: str | None  # None -> no match, entity is new (CreateNode)
    score: float
    method: str  # rules | embedding | llm | none


class _MatchJudgment(BaseModel):
    same_as_canonical_id: str | None = Field(
        description="canonical_id of the matching candidate, or null if none is the same real-world entity"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


_JUDGE_SYSTEM = """You are an entity-resolution judge for a knowledge graph.
Decide whether the NEW entity is the same real-world thing as one of the CANONICAL
candidates. Same thing under a different name/abbreviation -> match. Related but
distinct (a successor product, a sibling, a parent company, a different person with
a similar name) -> NOT a match. When genuinely uncertain, answer null: a false merge
corrupts the graph and is much harder to detect than a duplicate."""


def _llm_tier(entity: CandidateEntity, candidates: list[dict]) -> MatchResult:
    """LLM pairwise judgment for the ambiguous margin only — the expensive tier
    reserved for the hard 5-10% (§6.1)."""
    from kgi.llm import structured_call

    lines = [
        f"- canonical_id={c['canonical_id']!r} name={c['name']!r} "
        f"type={c['entity_type']!r} (cosine {c['score']:.2f})"
        for c in candidates
    ]
    evidence = "; ".join(s.quote for s in entity.evidence[:3])
    judgment = structured_call(
        "resolution-judge",
        _MatchJudgment,
        system=_JUDGE_SYSTEM,
        user=(
            f"NEW entity: name={entity.name!r} type={entity.entity_type!r}\n"
            f"Evidence: {evidence!r}\n\nCANONICAL candidates:\n" + "\n".join(lines)
        ),
    )
    valid_ids = {c["canonical_id"] for c in candidates}
    if judgment.same_as_canonical_id in valid_ids:
        return MatchResult(judgment.same_as_canonical_id, judgment.confidence, "llm")
    return MatchResult(None, judgment.confidence, "llm")


def resolve_entity(
    entity: CandidateEntity, graph: CanonicalGraph, vectors: EntityVectors
) -> MatchResult:
    # Tier 1 — rules: exact identifiers only (case-insensitive name, v1).
    match = graph.find_by_name(entity.name)
    if match:
        return MatchResult(match["id"], 1.0, "rules")

    # Tier 2 — embedding ANN blocking + cosine thresholds.
    hits = vectors.nearest(entity.name, limit=5)
    if not hits:
        return MatchResult(None, 0.0, "none")
    top = hits[0]
    if top["score"] >= EMBEDDING_ACCEPT:
        return MatchResult(top["canonical_id"], top["score"], "embedding")
    if top["score"] < EMBEDDING_REJECT:
        return MatchResult(None, top["score"], "none")

    # Tier 3 — LLM judgment on the ambiguous band.
    band = [h for h in hits if h["score"] >= EMBEDDING_REJECT]
    return _llm_tier(entity, band)


def resolve_entities(
    entities: list[CandidateEntity],
    graph: CanonicalGraph,
    vectors: EntityVectors,
) -> dict[str, MatchResult]:
    """Map each candidate temp_id to a canonical match (or None -> new node)."""
    return {e.temp_id: resolve_entity(e, graph, vectors) for e in entities}


def normalize_predicate(predicate: str, vectors: PredicateVectors) -> str:
    """Reuse a known predicate string when the new one is a paraphrase of it;
    otherwise keep the new predicate (it will be indexed at commit)."""
    hits = vectors.nearest(predicate, limit=1)
    if hits and hits[0]["score"] >= PREDICATE_NORMALIZE:
        return hits[0]["predicate"]
    return predicate
