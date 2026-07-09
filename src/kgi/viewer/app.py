"""Graph viewer API: the canonical graph plus the ingestion story around it —
provenance, review decisions, bi-temporal history, and the pending queue.

Read-only by design: this client *showcases* the system; mutations still go
through the pipeline + review gate.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from kgi.models import OpStatus, RouteTarget
from kgi.stores.neo4j import CanonicalGraph, StagingStore

_STATIC = Path(__file__).parent / "static"


class OpDecision(BaseModel):
    action: str = Field(pattern="^(accept|reject)$")
    note: str = ""


class ReviewRequest(BaseModel):
    reviewer: str = "web"
    decisions: dict[str, OpDecision]  # op_id -> decision; omitted ops are deferred


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

    @app.get("/api/search")
    def search(q: str, as_of: str | None = None):
        """Anchors + neighborhood facts — powers graph highlighting in the UI."""
        from kgi.retrieval import retrieve

        result = retrieve(q, as_of=as_of)
        return {
            "anchors": result["anchors"],
            "facts": [
                {"edge_id": f.edge_id, "text": f.render(), "sources": f.sources,
                 "support": f.support}
                for f in result["facts"]
            ],
        }

    @app.get("/api/ask")
    def ask(q: str, as_of: str | None = None):
        """Grounded answer with citations (edge ids let the UI highlight the graph)."""
        from kgi.retrieval import answer

        return answer(q, as_of=as_of)

    @app.post("/api/review/{patch_id}")
    def review(patch_id: str, req: ReviewRequest):
        """Apply reviewer decisions to a parked patch and resume its run — the
        browser-side face of the HITL gate. Omitted ops are deferred."""
        from langgraph.types import Command

        from kgi import observability
        from kgi.pipeline import durable_pipeline

        thread = staging.thread_for_patch(patch_id)
        if thread is None:
            raise HTTPException(404, f"no parked run for patch {patch_id}")
        decisions = {
            op_id: {"action": d.action, "note": d.note, "reviewer_id": req.reviewer}
            for op_id, d in req.decisions.items()
        }
        with durable_pipeline() as pipeline:
            config = {
                "configurable": {"thread_id": thread},
                "callbacks": observability.langgraph_callbacks(),
                "metadata": {"langfuse_session_id": thread, "patch_id": patch_id},
            }
            result = pipeline.invoke(Command(resume=decisions), config)
        return result.get("commit_report", {})

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
