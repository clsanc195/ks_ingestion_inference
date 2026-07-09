"""Graph viewer API: the canonical graph plus the ingestion story around it —
provenance, review decisions, bi-temporal history, and the pending queue.

Read-only by design: this client *showcases* the system; mutations still go
through the pipeline + review gate.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from kgi.models import OpStatus, RouteTarget
from kgi.stores.neo4j import CanonicalGraph, StagingStore

_STATIC = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="kgi viewer")
    graph = CanonicalGraph()
    staging = StagingStore()

    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    @app.get("/api/graph")
    def full_graph():
        """All canonical nodes and edges — superseded edges included, flagged."""
        with graph.session() as s:
            nodes = s.run(
                "MATCH (n:Canonical) "
                "RETURN n.id AS id, n.name AS name, n.entity_type AS type, "
                "COUNT { (n)-[:REL]-() } AS degree"
            ).data()
            edges = s.run(
                "MATCH (a:Canonical)-[r:REL]->(b:Canonical) "
                "RETURN r.id AS id, a.id AS source, b.id AS target, "
                "r.predicate AS predicate, r.valid_from AS valid_from, "
                "r.valid_to AS valid_to, r.support AS support, "
                "r.invalidation_reason AS reason"
            ).data()
        return {"nodes": nodes, "edges": edges}

    @app.get("/api/node/{canonical_id}")
    def node_detail(canonical_id: str):
        node = graph.get_node(canonical_id)
        if node is None:
            raise HTTPException(404, f"no canonical node {canonical_id}")
        with graph.session() as s:
            aliases = s.run(
                "MATCH (al:Alias)-[:SAME_AS]->(:Canonical {id: $id}) "
                "RETURN al.match_method AS method, al.match_score AS score",
                id=canonical_id,
            ).data()
            edges = s.run(
                "MATCH (a:Canonical {id: $id})-[r:REL]-(b:Canonical) "
                "RETURN r.predicate AS predicate, b.name AS other, "
                "startNode(r).id = $id AS outgoing, r.valid_from AS valid_from, "
                "r.valid_to AS valid_to, r.support AS support, "
                "r.invalidation_reason AS reason "
                "ORDER BY r.valid_to IS NOT NULL, predicate",
                id=canonical_id,
            ).data()
            provenance = s.run(
                "MATCH (op:AppliedOp)-[:WROTE]->(n {id: $id}) "
                "OPTIONAL MATCH (d:Document) WHERE d.doc_id = op.doc_id "
                "RETURN op.op_id AS op_id, op.patch_id AS patch_id, "
                "op.doc_id AS doc_id, d.source_uri AS source, op.at AS at "
                "ORDER BY op.at",
                id=canonical_id,
            ).data()
            op_ids = [p["op_id"] for p in provenance]
            decisions = s.run(
                "MATCH (dec:ReviewDecision) WHERE dec.op_id IN $ops "
                "RETURN dec.op_id AS op_id, dec.action AS action, "
                "dec.reviewer_id AS reviewer, dec.note AS note, "
                "dec.decided_at AS at",
                ops=op_ids,
            ).data()
        return {
            "node": node,
            "aliases": aliases,
            "edges": edges,
            "provenance": provenance,
            "decisions": decisions,
        }

    @app.get("/api/pending")
    def pending():
        """The review queue: what inference proposed but no human has admitted yet."""
        out = []
        for patch in staging.pending_patches():
            ops = [
                {
                    "op_id": r.op.op_id,
                    "kind": r.op.op,
                    "confidence": r.op.confidence,
                    "rationale": r.op.rationale,
                }
                for r in patch.ops
                if r.route == RouteTarget.review and r.status == OpStatus.pending
            ]
            out.append(
                {
                    "patch_id": patch.patch_id,
                    "doc_id": patch.doc_id,
                    "created_at": patch.created_at.isoformat(),
                    "ops": ops,
                }
            )
        return {"patches": out}

    @app.get("/api/stats")
    def stats():
        with graph.session() as s:
            row = s.run(
                "MATCH (n:Canonical) WITH count(n) AS nodes "
                "OPTIONAL MATCH ()-[r:REL]->() WHERE r.valid_to IS NULL "
                "WITH nodes, count(r) AS current "
                "OPTIONAL MATCH ()-[r2:REL]->() WHERE r2.valid_to IS NOT NULL "
                "WITH nodes, current, count(r2) AS superseded "
                "OPTIONAL MATCH (d:Document) WITH nodes, current, superseded, "
                "count(d) AS docs "
                "OPTIONAL MATCH (dec:ReviewDecision) "
                "RETURN nodes, current, superseded, docs, count(dec) AS decisions"
            ).single()
            stats = dict(row) if row else {}
        stats["pending_ops"] = sum(
            1
            for p in staging.pending_patches()
            for r in p.ops
            if r.route == RouteTarget.review and r.status == OpStatus.pending
        )
        return stats

    return app
