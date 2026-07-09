"""Ground-truth candidates from structured formats (BPMN): no LLM involved.

BPMN parse output is already a typed graph (§L0 key insight), so its triples enter
staging as candidates with extraction confidence 1.0 and the .bpmn file itself as
evidence. They still flow through resolution/routing/commit like everything else —
ground truth skips *extraction*, never the gate.
"""

import uuid

from kgi.models import CandidateEntity, CandidateRelation, EvidenceSpan, NormalizedDocument


def ground_truth_candidates(
    ndoc: NormalizedDocument,
) -> tuple[list[CandidateEntity], list[CandidateRelation]]:
    entities: dict[str, CandidateEntity] = {}  # by name
    relations: list[CandidateRelation] = []

    def _span(triple: dict) -> EvidenceSpan:
        return EvidenceSpan(
            doc_id=ndoc.doc.doc_id,
            unit_id=triple.get("block_id", ""),
            quote=f"{triple['subject']} {triple['predicate']} {triple['object']}",
        )

    def _ensure_entity(name: str, entity_type: str, triple: dict) -> CandidateEntity:
        if name not in entities:
            entities[name] = CandidateEntity(
                temp_id=f"ent_{uuid.uuid4().hex[:12]}",
                name=name,
                entity_type=entity_type,
                evidence=[_span(triple)],
                extraction_confidence=1.0,
            )
        return entities[name]

    # First pass: IS_A triples define entities and their (BPMN element) types.
    for t in ndoc.ground_truth_triples:
        if t["predicate"] == "IS_A":
            _ensure_entity(t["subject"], t["object"], t)

    # Second pass: structural relations between defined entities.
    for t in ndoc.ground_truth_triples:
        if t["predicate"] == "IS_A":
            continue
        subj = _ensure_entity(t["subject"], "Concept", t)
        obj = _ensure_entity(t["object"], "Concept", t)
        relations.append(
            CandidateRelation(
                temp_id=f"rel_{uuid.uuid4().hex[:12]}",
                subject_temp_id=subj.temp_id,
                predicate=t["predicate"],
                object_temp_id=obj.temp_id,
                properties={k: v for k, v in t.items()
                            if k not in ("subject", "predicate", "object", "block_id")
                            and v is not None},
                evidence=[_span(t)],
                extraction_confidence=1.0,
            )
        )

    return list(entities.values()), relations
