"""Neo4j access (L12): canonical graph + staging in one instance.

Data model:
  (:Canonical {id, name, entity_type, confidence, ...props})   — trusted entities
  (:Alias {id, name, entity_type})-[:SAME_AS]->(:Canonical)    — reversible merges (§7)
  (a:Canonical)-[:REL {id, predicate, valid_from, valid_to,
                       support, confidence}]->(b:Canonical)    — bi-temporal facts (L6)
  (:AppliedOp {op_id, patch_id, doc_id, run_id, at, ...})      — commit ledger (idempotency
      + provenance); [:WROTE]-> the node it touched, or edge_id property for edge ops
  (:Patch {id, doc_id, status, json, created_at})              — staged patches (L4)

Isolation rule: retrieval only ever matches :Canonical and current edges
(valid_to IS NULL). Staged patches live as :Patch documents, never as graph structure.
"""

import json

from neo4j import GraphDatabase

from kgi.config import settings
from kgi.models import Patch, ReviewDecision


_LUCENE_SPECIALS = set('+-&|!(){}[]^"~*?:\\/')


def _lucene_sanitize(query: str) -> str:
    """Strip Lucene operator characters so raw questions can't break the parser."""
    return "".join(c if c not in _LUCENE_SPECIALS else " " for c in query).strip()


class _Base:
    def __init__(self) -> None:
        cfg = settings()
        self._driver = GraphDatabase.driver(
            cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password)
        )

    def session(self):
        return self._driver.session()

    def close(self) -> None:
        self._driver.close()


class CanonicalGraph(_Base):
    def ensure_constraints(self) -> None:
        with self.session() as s:
            s.run("CREATE CONSTRAINT canonical_id IF NOT EXISTS "
                  "FOR (n:Canonical) REQUIRE n.id IS UNIQUE")
            s.run("CREATE CONSTRAINT alias_id IF NOT EXISTS "
                  "FOR (n:Alias) REQUIRE n.id IS UNIQUE")
            s.run("CREATE CONSTRAINT applied_op_id IF NOT EXISTS "
                  "FOR (n:AppliedOp) REQUIRE n.op_id IS UNIQUE")
            s.run("CREATE CONSTRAINT patch_id IF NOT EXISTS "
                  "FOR (n:Patch) REQUIRE n.id IS UNIQUE")
            # the lexical door: Lucene full-text (BM25 scoring), maintained
            # automatically by Neo4j on every write — no reindex job needed
            s.run("CREATE FULLTEXT INDEX entity_text IF NOT EXISTS "
                  "FOR (n:Canonical) ON EACH [n.name, n.description]")
            # r.quotes (string list) is indexed too — reinforcing sources'
            # quotes are lexically searchable, not just the first one.
            s.run("CREATE FULLTEXT INDEX fact_text IF NOT EXISTS "
                  "FOR ()-[r:REL]-() ON EACH [r.predicate, r.quote, r.quotes]")

    def get_node(self, canonical_id: str) -> dict | None:
        with self.session() as s:
            rec = s.run(
                "MATCH (n:Canonical {id: $id}) RETURN n", id=canonical_id
            ).single()
            return dict(rec["n"]) if rec else None

    def document_committed(self, content_hash: str) -> bool:
        """Has an identical document already been ingested and committed? (§3.1 step 1
        hash dedup — the idempotency NFR's first line of defense.)"""
        with self.session() as s:
            return s.run(
                "MATCH (d:Document {content_hash: $h}) RETURN d LIMIT 1", h=content_hash
            ).single() is not None

    def mark_document_committed(self, doc_id: str, content_hash: str, source_uri: str) -> None:
        with self.session() as s:
            s.run(
                "MERGE (d:Document {content_hash: $h}) "
                "SET d.doc_id = $doc_id, d.source_uri = $uri",
                h=content_hash, doc_id=doc_id, uri=source_uri,
            )

    def fulltext_entities(self, query: str, limit: int = 8) -> list[dict]:
        """BM25/lexical entity search over names + descriptions — exact rare terms
        (acronyms, codes, dates-as-text) that embeddings smear."""
        q = _lucene_sanitize(query)
        if not q:
            return []
        with self.session() as s:
            return s.run(
                "CALL db.index.fulltext.queryNodes('entity_text', $q) "
                "YIELD node, score RETURN node.id AS canonical_id, "
                "node.name AS name, score LIMIT $limit",
                q=q, limit=limit,
            ).data()

    def fulltext_facts(self, query: str, limit: int = 8) -> list[dict]:
        """BM25/lexical search over fact quotes + predicates."""
        q = _lucene_sanitize(query)
        if not q:
            return []
        with self.session() as s:
            return s.run(
                "CALL db.index.fulltext.queryRelationships('fact_text', $q) "
                "YIELD relationship, score "
                "RETURN relationship.id AS edge_id, "
                "startNode(relationship).id AS subject_id, "
                "endNode(relationship).id AS object_id, score LIMIT $limit",
                q=q, limit=limit,
            ).data()

    def find_by_name(self, name: str, entity_type: str | None = None) -> dict | None:
        """Case-insensitive exact-name lookup — the rules tier of resolution (L5).
        Only trivially-identical mentions match here; everything fuzzier belongs to
        the embedding/LLM tiers."""
        with self.session() as s:
            rec = s.run(
                "MATCH (n:Canonical) WHERE toLower(n.name) = toLower($name) "
                "AND ($type IS NULL OR n.entity_type = $type) "
                "RETURN n LIMIT 1",
                name=name, type=entity_type,
            ).single()
            return dict(rec["n"]) if rec else None

    def node_degree(self, canonical_id: str) -> int:
        with self.session() as s:
            rec = s.run(
                "MATCH (n:Canonical {id: $id}) "
                "RETURN COUNT { (n)-[:REL]-() } AS degree",
                id=canonical_id,
            ).single()
            return rec["degree"] if rec else 0

    def edges_between(
        self, subject_id: str, object_id: str, predicate: str | None = None
    ) -> list[dict]:
        """Current (valid_to IS NULL) canonical edges — input to correlation (L6)."""
        with self.session() as s:
            result = s.run(
                "MATCH (a:Canonical {id: $subj})-[r:REL]->(b:Canonical {id: $obj}) "
                "WHERE r.valid_to IS NULL "
                "AND ($pred IS NULL OR r.predicate = $pred) "
                "RETURN r",
                subj=subject_id, obj=object_id, pred=predicate,
            )
            return [dict(rec["r"]) for rec in result]

    def edges_from(self, subject_id: str, predicate: str) -> list[dict]:
        """Current edges (subject)-[predicate]->(*), with object ids — subject-side
        contradiction detection (L6): same subject, different object."""
        with self.session() as s:
            result = s.run(
                "MATCH (a:Canonical {id: $subj})-[r:REL {predicate: $pred}]->(b:Canonical) "
                "WHERE r.valid_to IS NULL "
                "RETURN r, b.id AS object_id, b.name AS object_name",
                subj=subject_id, pred=predicate,
            )
            return [{**dict(rec["r"]), "object_id": rec["object_id"],
                     "object_name": rec["object_name"]} for rec in result]

    def edges_to(self, object_id: str, predicate: str) -> list[dict]:
        """Current edges (*)-[predicate]->(object), with subject ids — object-side
        contradiction detection (L6): same object, different subject. This is the
        succession case: (Körner)-[ceo_of]->(CS) vs existing (Gottstein)-[ceo_of]->(CS)."""
        with self.session() as s:
            result = s.run(
                "MATCH (a:Canonical)-[r:REL {predicate: $pred}]->(b:Canonical {id: $obj}) "
                "WHERE r.valid_to IS NULL "
                "RETURN r, a.id AS subject_id, a.name AS subject_name",
                obj=object_id, pred=predicate,
            )
            return [{**dict(rec["r"]), "subject_id": rec["subject_id"],
                     "subject_name": rec["subject_name"]} for rec in result]

    def known_predicates(self) -> list[str]:
        """Distinct predicates in canonical — extraction guidance (L2) so new documents
        reuse existing vocabulary instead of coining paraphrases."""
        with self.session() as s:
            return [r["p"] for r in s.run(
                "MATCH ()-[r:REL]->() RETURN DISTINCT r.predicate AS p ORDER BY p"
            )]

    def known_entity_types(self) -> list[str]:
        with self.session() as s:
            return [r["t"] for r in s.run(
                "MATCH (n:Canonical) WHERE n.entity_type IS NOT NULL "
                "RETURN DISTINCT n.entity_type AS t ORDER BY t"
            )]

    def nodes_written_by_patch(self, patch_id: str) -> list[dict]:
        """Canonical nodes a committed patch wrote — the commit node indexes these
        into the entity vector store (only gate-approved facts get embedded)."""
        with self.session() as s:
            result = s.run(
                "MATCH (:AppliedOp {patch_id: $pid})-[:WROTE]->(n:Canonical) "
                "RETURN DISTINCT n",
                pid=patch_id,
            )
            return [dict(rec["n"]) for rec in result]

    def edges_written_by_patch(self, patch_id: str) -> list[dict]:
        """Full edge rows a committed patch wrote — indexed into the fact vectors."""
        with self.session() as s:
            return s.run(
                "MATCH (a:AppliedOp {patch_id: $pid}) WHERE a.edge_id IS NOT NULL "
                "MATCH (su:Canonical)-[r:REL {id: a.edge_id}]->(ob:Canonical) "
                "RETURN DISTINCT r.id AS edge_id, r.predicate AS predicate, "
                "coalesce(r.quotes, CASE WHEN r.quote IS NULL OR r.quote = '' "
                "  THEN [] ELSE [r.quote] END) AS quotes, r.valid_from AS valid_from, "
                "r.valid_to AS valid_to, su.id AS subject_id, su.name AS subject, "
                "ob.id AS object_id, ob.name AS object",
                pid=patch_id,
            ).data()

    def all_edges(self) -> list[dict]:
        """Every edge with endpoint info — the vector-rebuild source of truth."""
        with self.session() as s:
            return s.run(
                "MATCH (su:Canonical)-[r:REL]->(ob:Canonical) "
                "RETURN r.id AS edge_id, r.predicate AS predicate, "
                "coalesce(r.quotes, CASE WHEN r.quote IS NULL OR r.quote = '' "
                "  THEN [] ELSE [r.quote] END) AS quotes, r.valid_from AS valid_from, "
                "r.valid_to AS valid_to, su.id AS subject_id, su.name AS subject, "
                "ob.id AS object_id, ob.name AS object"
            ).data()

    def predicates_written_by_patch(self, patch_id: str) -> list[str]:
        with self.session() as s:
            result = s.run(
                "MATCH (a:AppliedOp {patch_id: $pid}) WHERE a.edge_id IS NOT NULL "
                "MATCH ()-[r:REL {id: a.edge_id}]->() RETURN DISTINCT r.predicate AS p",
                pid=patch_id,
            )
            return [rec["p"] for rec in result]

    def subgraph_for_review(self, canonical_ids: list[str], hops: int = 1) -> dict:
        """Neighborhood snapshot shown as the 'current' side of the review diff (L9)."""
        with self.session() as s:
            result = s.run(
                "MATCH (n:Canonical) WHERE n.id IN $ids "
                "OPTIONAL MATCH p = (n)-[:REL*1..%d]-(m:Canonical) "
                "WITH collect(DISTINCT n) + "
                "     coalesce(collect(DISTINCT last(nodes(p))), []) AS ns, "
                "     collect(DISTINCT relationships(p)) AS rss "
                "UNWIND ns AS node "
                "WITH collect(DISTINCT node) AS nodes, rss "
                "UNWIND CASE WHEN rss = [] THEN [[]] ELSE rss END AS rs "
                "UNWIND CASE WHEN rs = [] THEN [null] ELSE rs END AS rel "
                "RETURN nodes, collect(DISTINCT rel) AS rels" % hops,
                ids=canonical_ids,
            ).single()
            if result is None:
                return {"nodes": [], "edges": []}
            return {
                "nodes": [dict(n) for n in result["nodes"]],
                "edges": [dict(r) for r in result["rels"] if r is not None],
            }


class StagingStore(_Base):
    """Quarantine for pending patches (L4). The :Patch nodes double as the audit trail:
    status transitions pending -> committed / requeued are never deleted."""

    def save_patch(self, patch: Patch, status: str = "pending",
                   thread_id: str | None = None) -> None:
        with self.session() as s:
            s.run(
                "MERGE (p:Patch {id: $id}) "
                "SET p.doc_id = $doc_id, p.status = $status, "
                "    p.json = $json, p.created_at = $created_at, "
                "    p.thread_id = coalesce($thread_id, p.thread_id)",
                id=patch.patch_id,
                doc_id=patch.doc_id,
                status=status,
                json=patch.model_dump_json(),
                created_at=patch.created_at.isoformat(),
                thread_id=thread_id,
            )

    def thread_for_patch(self, patch_id: str) -> str | None:
        """The LangGraph thread a pending patch is parked on — `kgi review` resumes it."""
        with self.session() as s:
            rec = s.run(
                "MATCH (p:Patch {id: $id}) RETURN p.thread_id AS t", id=patch_id
            ).single()
            return rec["t"] if rec else None

    def save_decision(self, decision: "ReviewDecision") -> None:
        """Review decisions are provenance (L11) and active-learning labels (L15)."""
        with self.session() as s:
            s.run(
                "CREATE (d:ReviewDecision {id: $id, patch_id: $patch_id, op_id: $op_id, "
                "reviewer_id: $reviewer_id, action: $action, note: $note, "
                "decided_at: $decided_at})",
                id=decision.decision_id, patch_id=decision.patch_id,
                op_id=decision.op_id, reviewer_id=decision.reviewer_id,
                action=decision.action.value, note=decision.note,
                decided_at=decision.decided_at.isoformat(),
            )

    def load_patch(self, patch_id: str) -> Patch:
        with self.session() as s:
            rec = s.run("MATCH (p:Patch {id: $id}) RETURN p.json AS j", id=patch_id).single()
            if rec is None:
                raise KeyError(f"no patch {patch_id!r}")
            return Patch.model_validate(json.loads(rec["j"]))

    def set_status(self, patch_id: str, status: str) -> None:
        with self.session() as s:
            s.run("MATCH (p:Patch {id: $id}) SET p.status = $status",
                  id=patch_id, status=status)

    def pending_patches(self) -> list[Patch]:
        with self.session() as s:
            result = s.run(
                "MATCH (p:Patch {status: 'pending'}) "
                "RETURN p.json AS j ORDER BY p.created_at"
            )
            return [Patch.model_validate(json.loads(rec["j"])) for rec in result]
