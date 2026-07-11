"""Orchestration (L14): the ingest path (§3.1) as a durable LangGraph.

parse -> decompose -> extract -> resolve -> score_route -> stage -> [review] -> commit

The HITL gate is a LangGraph interrupt(): the run parks (hours/days) until the
reviewer resumes with Command(resume={op_id: {"action": ...}}). The Postgres
checkpointer makes that wait survive restarts (NFR: durability). Ops routed
auto_approve/auto_reject are settled in score_route; a patch with zero
review-routed ops goes straight to commit.

Resolution here is the *rules tier only* (exact-name match against canonical) —
enough to close the loop end-to-end. The embedding/LLM tiers land in
kgi.resolution.cascade and replace `_resolve_entity` below.
"""

import uuid
from datetime import datetime, timezone
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from kgi.models import (
    AssertEdge,
    CandidateEntity,
    CandidateRelation,
    CreateNode,
    ExtractionUnit,
    NodeRef,
    NormalizedDocument,
    OpStatus,
    Patch,
    Precondition,
    RouteTarget,
    UpdateNodeProps,
)
from kgi.models.patch import RoutedOp


class IngestState(TypedDict, total=False):
    source_path: str
    modality: str
    ndoc: NormalizedDocument
    units: list[ExtractionUnit]
    entities: list[CandidateEntity]
    relations: list[CandidateRelation]
    new_types: list[str]  # proposed types outside the approved schema (L3)
    patch: Patch
    signals: dict  # op_id -> confidence signals (extraction, resolution, new_type)
    commit_report: dict


def parse_node(state: IngestState) -> dict:
    from pathlib import Path

    from kgi.ingestion import get_parser
    from kgi.ingestion.base import register_document
    from kgi.models import Modality
    from kgi.stores.neo4j import CanonicalGraph

    path = Path(state["source_path"])
    modality = Modality(state["modality"])
    doc = register_document(path, modality)

    graph = CanonicalGraph()
    try:
        duplicate = graph.document_committed(doc.content_hash)
    finally:
        graph.close()
    if duplicate:
        return {
            "ndoc": NormalizedDocument(doc=doc, blocks=[]),
            "commit_report": {"duplicate_document": doc.doc_id},
        }
    return {"ndoc": get_parser(modality).parse(doc, path)}


def is_duplicate(state: IngestState) -> str:
    return "skip" if "duplicate_document" in state.get("commit_report", {}) else "continue"


def decompose_node(state: IngestState) -> dict:
    from kgi.decomposition import decompose

    return {"units": decompose(state["ndoc"])}


def extract_node(state: IngestState) -> dict:
    """v1: sequential unit extraction; upgrade path is Send() fan-out per unit.
    Ground-truth triples (BPMN) bypass the LLM entirely."""
    from kgi.extraction import extract_unit, ground_truth_candidates
    from kgi.schema import SchemaManager
    from kgi.stores.neo4j import CanonicalGraph

    schema = SchemaManager()
    graph = CanonicalGraph()
    try:
        # Guidance vocabulary = seed ontology + what canonical already uses, so new
        # documents reuse existing types/predicates instead of coining paraphrases.
        known_types = sorted({*schema.known_types, *graph.known_entity_types()})
        known_predicates = sorted({*schema.known_predicates, *graph.known_predicates()})
    finally:
        graph.close()

    entities, relations = ground_truth_candidates(state["ndoc"])
    total = len(state["units"])
    for i, unit in enumerate(state["units"], 1):
        print(f"  extract {i}/{total}: {unit.text[:60]!r}", flush=True)
        ents, rels = extract_unit(unit, known_types, known_predicates)
        entities.extend(ents)
        relations.extend(rels)
    print(f"  extracted {len(entities)} entities, {len(relations)} relations "
          f"from {total} units", flush=True)

    # "New" = never approved: not in the seed ontology and not already in canonical
    # (canonical types passed review once — they don't need re-gating per mention).
    new_types = sorted({e.entity_type for e in entities} - set(known_types))
    return {"entities": entities, "relations": relations, "new_types": new_types}


def _dedupe_batch(
    entities: list[CandidateEntity], relations: list[CandidateRelation]
) -> tuple[list[CandidateEntity], list[CandidateRelation]]:
    """Intra-batch rules-tier dedup: per-unit extraction re-mentions the same entity
    in many units, so merge exact-name duplicates (and the relations between them)
    before any canonical lookup. Evidence accumulates; it feeds the support signal."""
    survivors: dict[str, CandidateEntity] = {}
    alias: dict[str, str] = {}  # duplicate temp_id -> surviving temp_id
    for e in entities:
        key = e.name.strip().lower()
        if key in survivors:
            s = survivors[key]
            alias[e.temp_id] = s.temp_id
            s.evidence.extend(e.evidence)
            s.properties = {**e.properties, **s.properties}
            if e.extraction_confidence > s.extraction_confidence:
                s.entity_type = e.entity_type
                s.extraction_confidence = e.extraction_confidence
        else:
            survivors[key] = e

    merged: dict[tuple, CandidateRelation] = {}
    for r in relations:
        subj = alias.get(r.subject_temp_id, r.subject_temp_id)
        obj = alias.get(r.object_temp_id, r.object_temp_id)
        key = (subj, r.predicate.strip().lower(), obj)
        if key in merged:
            m = merged[key]
            m.evidence.extend(r.evidence)
            m.valid_from = m.valid_from or r.valid_from
            m.properties = {**r.properties, **m.properties}
            m.extraction_confidence = max(m.extraction_confidence, r.extraction_confidence)
        else:
            r.subject_temp_id, r.object_temp_id = subj, obj
            merged[key] = r
    return list(survivors.values()), list(merged.values())


def resolve_node(state: IngestState) -> dict:
    """Resolution cascade + minimal correlation -> proposed Patch (§3.1 step 6).

    Intra-batch dedup first, then per entity: rules (exact name) -> embedding cosine
    -> LLM judge. Rules match -> reuse the canonical id directly (plus UpdateNodeProps
    for gap-filling properties); embedding/LLM match -> MergeInto (SAME_AS provenance,
    reviewable); no match -> CreateNode. Predicates are paraphrase-normalized before
    correlation, so a known predicate reinforces instead of duplicating. AssertEdge
    between existing nodes carries an edge_absent precondition so a concurrent commit
    re-queues instead of duplicating.
    """
    from kgi.conflict import correlate_relation
    from kgi.models import MergeInto
    from kgi.resolution import normalize_predicate, resolve_entity
    from kgi.stores.neo4j import CanonicalGraph
    from kgi.stores.qdrant import EntityVectors, PredicateVectors

    graph = CanonicalGraph()
    entity_vectors, predicate_vectors = EntityVectors(), PredicateVectors()
    ops: list[RoutedOp] = []
    signals: dict[str, dict] = {}
    refs: dict[str, NodeRef] = {}  # temp_id -> how edges should reference this entity
    dep_of: dict[str, str] = {}  # temp_id -> op_id that materializes it
    new_types = set(state.get("new_types", []))

    def _add(op, *, extraction: float, resolution: float, new_type: bool,
             support: int = 1, semantic: bool = False) -> None:
        ops.append(RoutedOp(op=op, route=RouteTarget.review))
        signals[op.op_id] = {
            "extraction": extraction,
            "resolution": resolution,
            "new_type": new_type,
            "support": support,
            "semantic": semantic,
        }

    # Normalize predicates BEFORE intra-batch dedup, so two units phrasing the same
    # fact differently ("produces" / "has flagship product") collapse into one op
    # instead of colliding at commit via their edge_absent preconditions.
    for rel in state["relations"]:
        rel.predicate = normalize_predicate(rel.predicate, predicate_vectors)
    entities, relations = _dedupe_batch(state["entities"], state["relations"])

    try:
        for ent in entities:
            result = resolve_entity(ent, graph, entity_vectors)
            is_new_type = ent.entity_type in new_types
            if result.canonical_id and result.method == "rules":
                refs[ent.temp_id] = NodeRef(canonical_id=result.canonical_id)
                match = graph.get_node(result.canonical_id) or {}
                gap_props = {k: v for k, v in ent.properties.items() if k not in match}
                if gap_props:
                    _add(
                        UpdateNodeProps(
                            op_id=f"op_{uuid.uuid4().hex[:12]}",
                            canonical_id=result.canonical_id,
                            properties=gap_props,
                            preconditions=[
                                Precondition(kind="node_exists", subject=result.canonical_id)
                            ],
                            rationale=f"extends existing '{ent.name}' with {sorted(gap_props)}",
                        ),
                        extraction=ent.extraction_confidence, resolution=1.0,
                        new_type=is_new_type, support=len(ent.evidence),
                    )
            elif result.canonical_id:
                # Non-trivial match (embedding/LLM): reviewable MergeInto for
                # SAME_AS provenance; edges land on the canonical node.
                refs[ent.temp_id] = NodeRef(canonical_id=result.canonical_id)
                canonical_name = (graph.get_node(result.canonical_id) or {}).get("name", "?")
                _add(
                    MergeInto(
                        op_id=f"op_{uuid.uuid4().hex[:12]}",
                        temp_id=ent.temp_id,
                        canonical_id=result.canonical_id,
                        match_score=result.score,
                        match_method=result.method,
                        preconditions=[
                            Precondition(kind="node_exists", subject=result.canonical_id)
                        ],
                        rationale=(
                            f"'{ent.name}' resolved to existing '{canonical_name}' "
                            f"({result.method}, {result.score:.2f})"
                        ),
                    ),
                    extraction=ent.extraction_confidence, resolution=result.score,
                    new_type=is_new_type, support=len(ent.evidence),
                )
            else:
                op_id = f"op_{uuid.uuid4().hex[:12]}"
                refs[ent.temp_id] = NodeRef(temp_id=ent.temp_id)
                dep_of[ent.temp_id] = op_id
                _add(
                    CreateNode(
                        op_id=op_id,
                        temp_id=ent.temp_id,
                        entity_type=ent.entity_type,
                        properties={"name": ent.name, **ent.properties},
                        rationale=f"new entity '{ent.name}' ({ent.entity_type})",
                    ),
                    extraction=ent.extraction_confidence, resolution=0.5,
                    new_type=is_new_type, support=len(ent.evidence),
                )

        names_by_temp = {e.temp_id: e.name for e in entities}

        def _ref_name(ref: NodeRef, temp_id: str) -> str:
            if ref.temp_id:
                return names_by_temp.get(temp_id, "?")
            return (graph.get_node(ref.canonical_id) or {}).get("name", "?")

        for rel in relations:
            subj, obj = refs[rel.subject_temp_id], refs[rel.object_temp_id]
            depends = [dep_of[t] for t in (rel.subject_temp_id, rel.object_temp_id)
                       if t in dep_of]
            if subj.canonical_id or obj.canonical_id:
                # At least one endpoint exists in canonical: full correlation,
                # including contradiction detection on whichever side is shared —
                # subject-side (a company moved HQ) or object-side (a role changed
                # hands: new CEO node pointing at an existing company).
                _outcome, correlated = correlate_relation(
                    graph, rel,
                    subj, _ref_name(subj, rel.subject_temp_id),
                    obj, _ref_name(obj, rel.object_temp_id),
                    depends,
                )
                for item in correlated:
                    _add(
                        item.op,
                        extraction=rel.extraction_confidence,
                        resolution=item.resolution,
                        new_type=False, support=len(rel.evidence),
                        semantic=item.semantic,
                    )
                continue
            # Both endpoints are new this patch: nothing in canonical to contradict —
            # plain assertion, dependent on its CreateNode ops.
            _add(
                AssertEdge(
                    op_id=f"op_{uuid.uuid4().hex[:12]}",
                    depends_on=depends,
                    subject=subj,
                    predicate=rel.predicate,
                    object=obj,
                    properties=rel.properties,
                    valid_from=rel.valid_from,
                    rationale=f"asserts {rel.predicate}",
                ),
                extraction=rel.extraction_confidence, resolution=0.5,
                new_type=False, support=len(rel.evidence),
            )
    finally:
        graph.close()
        entity_vectors.close()
        predicate_vectors.close()

    patch = Patch(
        patch_id=f"patch_{uuid.uuid4().hex[:12]}",
        doc_id=state["ndoc"].doc.doc_id,
        extraction_run_id=f"run_{uuid.uuid4().hex[:12]}",
        created_at=datetime.now(timezone.utc),
        ops=ops,
    )
    return {"patch": patch, "signals": signals}


def score_route_node(state: IngestState) -> dict:
    """L7 scoring + L8 routing. auto_approve -> approved, auto_reject -> rejected;
    only review-routed ops reach the interrupt."""
    from kgi.confidence import score_op
    from kgi.routing import route_op

    patch, signals = state["patch"], state["signals"]
    for routed in patch.ops:
        sig = signals[routed.op.op_id]
        routed.op.confidence = score_op(
            routed.op,
            extraction_confidence=sig["extraction"],
            support_count=sig.get("support", 1),
            resolution_score=sig["resolution"],
            validation_passed=not sig["new_type"],
        )
        routed.route = route_op(
            routed.op,
            introduces_new_type=sig["new_type"],
            is_semantic_conflict=sig.get("semantic", False),
        )
        if routed.route == RouteTarget.auto_approve:
            routed.status = OpStatus.approved
        elif routed.route == RouteTarget.auto_reject:
            routed.status = OpStatus.rejected
    return {"patch": patch}


def stage_node(state: IngestState, config: "RunnableConfig") -> dict:
    """Candidates land in staging as a routed patch — nothing has touched canonical.
    The run's thread_id is stored with the patch so `kgi review` can resume it."""
    from kgi.stores.neo4j import StagingStore

    staging = StagingStore()
    try:
        staging.save_patch(
            state["patch"],
            thread_id=config.get("configurable", {}).get("thread_id"),
        )
    finally:
        staging.close()
    return {}


def needs_review(state: IngestState) -> str:
    pending_review = any(
        r.route == RouteTarget.review and r.status == OpStatus.pending
        for r in state["patch"].ops
    )
    return "review" if pending_review else "commit"


def review_node(state: IngestState) -> dict:
    """The HITL gate (L9). Parks until the reviewer resumes with
    {op_id: {"action": "accept"|"reject"|"edit", "edited_op": {...},
             "reviewer_id": str, "note": str}}."""
    import uuid as _uuid
    from datetime import datetime, timezone

    from kgi.models import ReviewAction, ReviewDecision
    from kgi.stores.neo4j import StagingStore

    patch = state["patch"]
    decisions = interrupt(
        {
            "patch_id": patch.patch_id,
            "doc_id": patch.doc_id,
            "ops": [
                r.model_dump()
                for r in patch.ops
                if r.route == RouteTarget.review and r.status == OpStatus.pending
            ],
        }
    )
    staging = StagingStore()
    try:
        for routed in patch.ops:
            if routed.route != RouteTarget.review or routed.status != OpStatus.pending:
                continue
            d = decisions.get(routed.op.op_id)
            if d is None:
                continue  # defer: stays pending, held back at commit
            action = d["action"]
            if action in ("accept", "force_merge"):
                routed.status = OpStatus.approved
            elif action == "edit":
                routed.op = type(routed.op).model_validate(
                    {**routed.op.model_dump(), **d["edited_op"]}
                )
                routed.status = OpStatus.approved
            elif action == "reject":
                routed.status = OpStatus.rejected
            staging.save_decision(
                ReviewDecision(
                    decision_id=f"dec_{_uuid.uuid4().hex[:12]}",
                    patch_id=patch.patch_id,
                    op_id=routed.op.op_id,
                    reviewer_id=d.get("reviewer_id", "unknown"),
                    action=ReviewAction(action),
                    note=d.get("note", ""),
                    decided_at=datetime.now(timezone.utc),
                )
            )
    finally:
        staging.close()
    return {"patch": patch}


def commit_node(state: IngestState) -> dict:
    """§3.1 step 10: the only path into canonical. Persists final statuses to staging."""
    from kgi.commit import commit_patch
    from kgi.stores.neo4j import CanonicalGraph, StagingStore

    graph, staging = CanonicalGraph(), StagingStore()
    try:
        graph.ensure_constraints()
        report = commit_patch(graph, state["patch"])
        remaining = any(
            r.route == RouteTarget.review and r.status == OpStatus.pending
            for r in state["patch"].ops
        )
        status = ("pending" if remaining
                  else "requeued" if report.requeued else "committed")
        staging.save_patch(state["patch"], status=status)
        if not report.requeued and not remaining:
            doc = state["ndoc"].doc
            graph.mark_document_committed(doc.doc_id, doc.content_hash, doc.source_uri)
        if report.committed:
            # Index only what survived the gate: committed entities + predicates
            # become searchable for the next document's resolution cascade.
            from kgi.stores.qdrant import EntityVectors, PredicateVectors

            entity_vectors, predicate_vectors = EntityVectors(), PredicateVectors()
            try:
                for node in graph.nodes_written_by_patch(state["patch"].patch_id):
                    if node.get("name"):
                        entity_vectors.upsert_entity(
                            node["id"], node["name"], node.get("entity_type", "")
                        )
                for predicate in graph.predicates_written_by_patch(state["patch"].patch_id):
                    predicate_vectors.upsert_predicate(predicate)
            finally:
                entity_vectors.close()
                predicate_vectors.close()
    finally:
        graph.close()
        staging.close()
    return {
        "commit_report": {
            "committed": report.committed,
            "requeued": report.requeued,
            "blocked": report.blocked,
            "skipped": report.skipped,
        }
    }


def build_pipeline(checkpointer=None):
    g = StateGraph(IngestState)
    g.add_node("parse", parse_node)
    g.add_node("decompose", decompose_node)
    g.add_node("extract", extract_node)
    g.add_node("resolve", resolve_node)
    g.add_node("score_route", score_route_node)
    g.add_node("stage", stage_node)
    g.add_node("review", review_node)
    g.add_node("commit", commit_node)

    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", is_duplicate, {"skip": END, "continue": "decompose"})
    g.add_edge("decompose", "extract")
    g.add_edge("extract", "resolve")
    g.add_edge("resolve", "score_route")
    g.add_edge("score_route", "stage")
    g.add_conditional_edges("stage", needs_review, {"review": "review", "commit": "commit"})
    g.add_edge("review", "commit")
    # Deferred ops loop back to the gate: commit what was decided, park again for
    # the rest. A patch only closes when nothing review-routed is left pending.
    g.add_conditional_edges(
        "commit",
        lambda state: "review" if needs_review(state) == "review" else "done",
        {"review": "review", "done": END},
    )

    return g.compile(checkpointer=checkpointer or MemorySaver())
