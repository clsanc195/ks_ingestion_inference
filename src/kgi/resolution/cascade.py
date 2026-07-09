"""Entity resolution cascade (L5): rules -> embedding cosine -> LLM pairwise.

Open-domain note (§7): the rules tier only covers explicit identifiers (URLs, emails,
doc-internal IDs), so embedding + LLM tiers carry most of the load. Clustering uses
Louvain (via Neo4j GDS) to cut weak SAME_AS edges before merging — never plain
transitive closure (over-merge risk, §6).
"""

from dataclasses import dataclass

from kgi.models import CandidateEntity


@dataclass
class MatchResult:
    canonical_id: str | None  # None -> no match, entity is new (CreateNode)
    score: float
    method: str  # rules | embedding | llm | none


# Escalation thresholds between cascade tiers (distinct from L8 routing thresholds):
# embedding score >= ACCEPT -> match; <= REJECT -> new entity; in between -> LLM tier.
EMBEDDING_ACCEPT = 0.90
EMBEDDING_REJECT = 0.60


def _rules_tier(entity: CandidateEntity) -> MatchResult | None:
    """Exact-identifier matching only (open-domain). Returns None to escalate."""
    raise NotImplementedError


def _embedding_tier(entity: CandidateEntity) -> MatchResult | None:
    """ANN over Qdrant entity embeddings (blocking) + cosine threshold (matching).
    Returns None for the ambiguous band [EMBEDDING_REJECT, EMBEDDING_ACCEPT]."""
    raise NotImplementedError


def _llm_tier(entity: CandidateEntity, candidates: list[str]) -> MatchResult:
    """LLM pairwise judgment with chain-of-thought for the ambiguous margin only —
    the expensive tier reserved for the hard 5-10% (§6.1)."""
    raise NotImplementedError


def resolve_entities(entities: list[CandidateEntity]) -> dict[str, MatchResult]:
    """Map each candidate temp_id to a canonical match (or None -> new node)."""
    results: dict[str, MatchResult] = {}
    for e in entities:
        result = _rules_tier(e) or _embedding_tier(e) or _llm_tier(e, candidates=[])
        results[e.temp_id] = result
    return results
