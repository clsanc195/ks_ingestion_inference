"""Extraction engine (L2): open triple extraction with structured output (Instructor).

Open-domain decision (§7): the induced schema is passed as *guidance*, not a hard
filter — the model may propose types outside it; those become schema-extension
candidates that are themselves HITL-gated (L3).
"""

import uuid

from pydantic import BaseModel, Field

from kgi.llm import structured_call
from kgi.models import CandidateEntity, CandidateRelation, EvidenceSpan, ExtractionUnit

PROMPT_VERSION = "v1"


class _ExtractedEntity(BaseModel):
    name: str
    entity_type: str = Field(description="Prefer a known type; propose a new one if none fits")
    properties: dict = Field(default_factory=dict)
    quote: str = Field(description="Verbatim supporting text from the unit")
    confidence: float = Field(ge=0.0, le=1.0)


class _ExtractedRelation(BaseModel):
    subject: str
    predicate: str
    object: str
    valid_from: str | None = Field(default=None, description="ISO date if the text states one")
    quote: str
    confidence: float = Field(ge=0.0, le=1.0)


class _ExtractionResult(BaseModel):
    entities: list[_ExtractedEntity]
    relations: list[_ExtractedRelation]


_SYSTEM = """You extract knowledge-graph candidates from one unit of text.
Return every entity and relation the text explicitly asserts — nothing implied or
inferred beyond the text. Each item must include a verbatim supporting quote.

Entities must be specific, nameable things: proper names, titles, organizations,
places, products, dated events. NEVER output as an entity:
- pronouns (he, she, it, they, this, ...)
- unnamed references ("the city", "the film", "the company", "the band")
If the text refers to something only by pronoun or generic reference, resolve it to
the named entity when the name appears in this unit or its context; if it cannot be
resolved to a name, OMIT that entity and every fact involving it. A dropped fact is
better than a fact attached to nobody.

Known entity types (guidance, not a closed list): {types}
Known predicates (guidance, not a closed list): {predicates}"""


def extract_unit(
    unit: ExtractionUnit,
    known_types: list[str],
    known_predicates: list[str],
) -> tuple[list[CandidateEntity], list[CandidateRelation]]:
    result = structured_call(
        "extract-unit",
        _ExtractionResult,
        system=_SYSTEM.format(
            types=", ".join(known_types) or "(none yet)",
            predicates=", ".join(known_predicates) or "(none yet)",
        ),
        user=f"Context: {unit.context}\n\nText:\n{unit.text}",
        max_tokens=4096,
    )

    def _span(quote: str) -> EvidenceSpan:
        return EvidenceSpan(doc_id=unit.doc_id, unit_id=unit.unit_id, quote=quote)

    entities: list[CandidateEntity] = []
    by_name: dict[str, str] = {}
    for e in result.entities:
        temp_id = f"ent_{uuid.uuid4().hex[:12]}"
        by_name[e.name] = temp_id
        entities.append(
            CandidateEntity(
                temp_id=temp_id,
                name=e.name,
                entity_type=e.entity_type,
                properties=e.properties,
                evidence=[_span(e.quote)],
                extraction_confidence=e.confidence,
            )
        )

    relations = [
        CandidateRelation(
            temp_id=f"rel_{uuid.uuid4().hex[:12]}",
            subject_temp_id=by_name[r.subject],
            predicate=r.predicate,
            object_temp_id=by_name[r.object],
            valid_from=r.valid_from,
            evidence=[_span(r.quote)],
            extraction_confidence=r.confidence,
        )
        for r in result.relations
        if r.subject in by_name and r.object in by_name
    ]
    entities, relations = _drop_unnamed(entities, relations)
    return entities, relations


_PRONOUNS = {
    "he", "she", "it", "they", "them", "him", "her", "his", "hers", "its",
    "their", "theirs", "this", "that", "these", "those", "who", "which",
}
_GENERIC_LEAD = ("the ", "a ", "an ", "this ", "that ", "these ", "those ",
                 "his ", "her ", "their ", "its ")


def _is_unnamed(name: str) -> bool:
    """Pronouns and all-lowercase determiner phrases ('the city') are references,
    not entities. Capitalized titles ('The Chronicles of Riddick') survive."""
    stripped = name.strip()
    if stripped.lower() in _PRONOUNS:
        return True
    low = stripped.lower()
    return low == stripped and low.startswith(_GENERIC_LEAD)


def _drop_unnamed(
    entities: list[CandidateEntity], relations: list[CandidateRelation]
) -> tuple[list[CandidateEntity], list[CandidateRelation]]:
    """Safety net behind the prompt: no pronoun/generic-reference entities, and no
    relations left dangling on them."""
    dropped = {e.temp_id for e in entities if _is_unnamed(e.name)}
    if dropped:
        names = [e.name for e in entities if e.temp_id in dropped]
        print(f"  dropped unnamed entities: {names}", flush=True)
    kept_entities = [e for e in entities if e.temp_id not in dropped]
    kept_relations = [
        r for r in relations
        if r.subject_temp_id not in dropped and r.object_temp_id not in dropped
    ]
    return kept_entities, kept_relations
