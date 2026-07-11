"""Retrieval (L13), local-search style: anchor entities by vector similarity, expand
their neighborhood with temporal filtering, return facts with provenance.

Temporal semantics (the payoff of bi-temporal edges, L6):
  as_of=None  -> current knowledge: only edges with valid_to IS NULL
  as_of=DATE  -> what was true then: valid_from <= DATE < valid_to (open ends pass)

Dates are ISO-prefix strings ("2019", "2026-03", "2026-07-08"); lexicographic
comparison is correct for that family.

Global search (community summaries) and cross-encoder reranking are deliberately
deferred until the graph is large enough to need them.
"""

from dataclasses import dataclass, field

from kgi.stores.neo4j import CanonicalGraph
from kgi.stores.qdrant import EntityVectors

ANCHOR_MIN_SCORE = 0.45  # below this the query simply isn't about the entity
FACT_MIN_SCORE = 0.40    # fact-vector matches (the Facts door) seeding the walk
MAX_SEEDS = 10
_TEMPORAL_FILTER = (
    "($as_of IS NULL AND r.valid_to IS NULL) OR "
    "($as_of IS NOT NULL AND (r.valid_from IS NULL OR r.valid_from <= $as_of) "
    "AND (r.valid_to IS NULL OR r.valid_to > $as_of))"
)


@dataclass
class Fact:
    edge_id: str
    subject: str
    predicate: str
    object: str
    valid_from: str | None
    valid_to: str | None
    support: int
    subject_id: str = ""
    object_id: str = ""
    # Verbatim supporting text, one entry per source that asserted the fact —
    # the first at assert time, the rest accumulated by reinforce ops.
    quotes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # source document URIs

    def render(self) -> str:
        window = ""
        if self.valid_from or self.valid_to:
            window = f" [{self.valid_from or '…'} → {self.valid_to or 'now'}]"
        return f"({self.subject}) —{self.predicate}→ ({self.object}){window}"


def find_anchor_entities(query: str, k: int = 4) -> list[dict]:
    """Entities the query is about: vector similarity over canonical entity names."""
    vectors = EntityVectors()
    try:
        hits = vectors.nearest(query, limit=k)
    finally:
        vectors.close()
    return [h for h in hits if h["score"] >= ANCHOR_MIN_SCORE]


def find_matching_facts(query: str, k: int = 8) -> list[dict]:
    """The Facts door: match the question against committed facts directly —
    catches fact-shaped queries ("what happened on <date>?", "any awards won?")
    that have no entity to anchor on."""
    from kgi.stores.qdrant import FactVectors

    vectors = FactVectors()
    try:
        hits = vectors.nearest(query, limit=k)
    finally:
        vectors.close()
    return [h for h in hits if h["score"] >= FACT_MIN_SCORE]


def neighborhood_facts(
    graph: CanonicalGraph,
    anchor_ids: list[str],
    hops: int = 2,
    as_of: str | None = None,
    limit: int = 60,
) -> list[Fact]:
    """Expand hop-by-hop from the anchors, applying the temporal filter per edge
    (variable-length Cypher can't filter each edge against as_of, so we iterate)."""
    facts: dict[str, Fact] = {}
    frontier = list(anchor_ids)
    seen_nodes = set(anchor_ids)
    with graph.session() as s:
        for _ in range(hops):
            if not frontier or len(facts) >= limit:
                break
            rows = s.run(
                "MATCH (a:Canonical)-[r:REL]-(b:Canonical) WHERE a.id IN $ids "
                f"AND ({_TEMPORAL_FILTER}) "
                "RETURN DISTINCT r.id AS edge_id, startNode(r).id AS subj_id, "
                "startNode(r).name AS subject, r.predicate AS predicate, "
                "endNode(r).name AS object, endNode(r).id AS obj_id, "
                "r.valid_from AS valid_from, r.valid_to AS valid_to, "
                "coalesce(r.support, 1) AS support, "
                "coalesce(r.quotes, CASE WHEN r.quote IS NULL OR r.quote = '' "
                "  THEN [] ELSE [r.quote] END) AS quotes",
                ids=frontier, as_of=as_of,
            ).data()
            next_frontier = []
            for row in rows:
                if len(facts) >= limit:
                    break
                if row["edge_id"] not in facts:
                    facts[row["edge_id"]] = Fact(
                        edge_id=row["edge_id"], subject=row["subject"],
                        predicate=row["predicate"], object=row["object"],
                        subject_id=row["subj_id"], object_id=row["obj_id"],
                        valid_from=row["valid_from"], valid_to=row["valid_to"],
                        support=row["support"], quotes=row["quotes"],
                    )
                for node_id in (row["subj_id"], row["obj_id"]):
                    if node_id not in seen_nodes:
                        seen_nodes.add(node_id)
                        next_frontier.append(node_id)
            frontier = next_frontier

        if facts:  # provenance: which committed documents wrote each edge
            rows = s.run(
                "MATCH (op:AppliedOp) WHERE op.edge_id IN $ids "
                "OPTIONAL MATCH (d:Document) WHERE d.doc_id = op.doc_id "
                "RETURN op.edge_id AS edge_id, "
                "collect(DISTINCT coalesce(d.source_uri, op.doc_id)) AS sources",
                ids=list(facts),
            ).data()
            for row in rows:
                facts[row["edge_id"]].sources = row["sources"]
    return list(facts.values())


def _rrf_seeds(ranked_lists: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion: scores are incomparable across doors (cosine vs
    BM25), ranks aren't. Each node id earns 1/(k+rank) per list it appears in."""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        seen: set[str] = set()
        rank = 0
        for nid in lst:
            if nid in seen:
                continue
            seen.add(nid)
            rank += 1
            scores[nid] = scores.get(nid, 0.0) + 1.0 / (k + rank)
    return [nid for nid, _ in sorted(scores.items(), key=lambda x: -x[1])]


def retrieve(query: str, as_of: str | None = None, hops: int = 2) -> dict:
    """Three doors, one walk: entity vectors (meaning), fact vectors (what
    happened), Lucene full-text (exact terms) — fused by RRF into walk seeds."""
    anchors = find_anchor_entities(query)
    fact_hits = find_matching_facts(query)

    graph = CanonicalGraph()
    try:
        lex_entities = graph.fulltext_entities(query)
        lex_facts = graph.fulltext_facts(query)

        seeds = _rrf_seeds([
            [a["canonical_id"] for a in anchors],
            [sid for h in fact_hits for sid in (h["subject_id"], h["object_id"])],
            [e["canonical_id"] for e in lex_entities],
            [sid for h in lex_facts for sid in (h["subject_id"], h["object_id"])],
        ])[:MAX_SEEDS]

        facts = neighborhood_facts(graph, seeds, hops=hops, as_of=as_of)
    finally:
        graph.close()
    return {"anchors": anchors, "fact_hits": fact_hits,
            "lexical": {"entities": lex_entities, "facts": lex_facts},
            "facts": facts}
