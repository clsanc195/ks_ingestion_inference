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
                "coalesce(r.quotes, CASE WHEN r.quote IS NULL OR r.quote = '' "
                "  THEN [] ELSE [r.quote] END) AS quotes, "
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
        """The review queue, enriched for the review board: each op carries a
        human-readable fact text, a primary-entity group label, and a conflict flag."""
        out = []
        for patch in staging.pending_patches():
            temp_names: dict[str, str] = {}
            canonical_ids: set[str] = set()
            edge_ids: set[str] = set()
            for r in patch.ops:
                op = r.op
                if op.op == "create_node":
                    temp_names[op.temp_id] = op.properties.get("name", op.temp_id)
                elif op.op == "merge_into":
                    canonical_ids.add(op.canonical_id)
                elif op.op in ("assert_edge",):
                    for ref in (op.subject, op.object):
                        if ref.canonical_id:
                            canonical_ids.add(ref.canonical_id)
                elif op.op in ("invalidate_edge", "reinforce_edge"):
                    edge_ids.add(op.canonical_edge_id)
                elif op.op == "update_node_props":
                    canonical_ids.add(op.canonical_id)

            canon_names: dict[str, str] = {}
            edge_info: dict[str, dict] = {}
            with graph.session() as s:
                if canonical_ids:
                    for rec in s.run(
                        "MATCH (n:Canonical) WHERE n.id IN $ids RETURN n.id AS id, n.name AS name",
                        ids=list(canonical_ids),
                    ):
                        canon_names[rec["id"]] = rec["name"]
                if edge_ids:
                    for rec in s.run(
                        "MATCH (a:Canonical)-[r:REL]->(b:Canonical) WHERE r.id IN $ids "
                        "RETURN r.id AS id, a.name AS s, r.predicate AS p, b.name AS o",
                        ids=list(edge_ids),
                    ):
                        edge_info[rec["id"]] = dict(rec)

            def ref_name(ref) -> str:
                if ref.canonical_id:
                    return canon_names.get(ref.canonical_id, ref.canonical_id)
                return temp_names.get(ref.temp_id, ref.temp_id or "?")

            ops = []
            for r in patch.ops:
                if not (r.route == RouteTarget.review and r.status == OpStatus.pending):
                    continue
                op = r.op
                if op.op == "create_node":
                    group = op.properties.get("name", op.temp_id)
                    text = f"new entity: {group} ({op.entity_type})"
                elif op.op == "merge_into":
                    group = canon_names.get(op.canonical_id, op.canonical_id)
                    text = f"merge {temp_names.get(op.temp_id, op.temp_id)} → {group} ({op.match_method} {op.match_score:.2f})"
                elif op.op == "assert_edge":
                    group = ref_name(op.subject)
                    text = f"({group}) —{op.predicate}→ ({ref_name(op.object)})"
                    if op.valid_from:
                        text += f" · from {op.valid_from}"
                elif op.op in ("invalidate_edge", "reinforce_edge"):
                    e = edge_info.get(op.canonical_edge_id, {})
                    group = e.get("s", "?")
                    verb = "close" if op.op == "invalidate_edge" else "reinforce"
                    text = f"{verb}: ({e.get('s','?')}) —{e.get('p','?')}→ ({e.get('o','?')})"
                elif op.op == "update_node_props":
                    group = canon_names.get(op.canonical_id, op.canonical_id)
                    text = f"update {group}: {op.properties}"
                else:
                    group, text = "other", op.op
                ops.append({
                    "op_id": op.op_id,
                    "kind": op.op,
                    "confidence": op.confidence,
                    "rationale": op.rationale,
                    "group": group,
                    "text": text,
                    "flag": "conflict" if ("CONFLICT" in op.rationale.upper()
                                           or "contested" in op.rationale) else "",
                })
            out.append({
                "patch_id": patch.patch_id,
                "doc_id": patch.doc_id,
                "created_at": patch.created_at.isoformat(),
                "ops": ops,
            })
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
            with observability.span(f"kgi-review {patch_id}",
                                    {"decisions": len(decisions),
                                     "reviewer": req.reviewer}) as sp:
                result = pipeline.invoke(Command(resume=decisions), config)
                if sp is not None:
                    sp.update(output=result.get("commit_report"))
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
