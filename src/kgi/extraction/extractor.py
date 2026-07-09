"""Extraction engine (L2): open triple extraction with structured output (Instructor).

Open-domain decision (§7): the induced schema is passed as *guidance*, not a hard
filter — the model may propose types outside it; those become schema-extension
candidates that are themselves HITL-gated (L3).
"""

import uuid

import anthropic
import instructor
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from kgi.config import settings
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

Known entity types (guidance, not a closed list): {types}
Known predicates (guidance, not a closed list): {predicates}"""


def extract_unit(
    unit: ExtractionUnit,
    known_types: list[str],
    known_predicates: list[str],
) -> tuple[list[CandidateEntity], list[CandidateRelation]]:
    load_dotenv()  # ANTHROPIC_API_KEY may live in .env rather than the shell
    client = instructor.from_anthropic(anthropic.Anthropic())
    result = client.chat.completions.create(
        model=settings().extraction_model,
        max_tokens=4096,
        response_model=_ExtractionResult,
        messages=[
            {
                "role": "system",
                "content": _SYSTEM.format(
                    types=", ".join(known_types) or "(none yet)",
                    predicates=", ".join(known_predicates) or "(none yet)",
                ),
            },
            {"role": "user", "content": f"Context: {unit.context}\n\nText:\n{unit.text}"},
        ],
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
    return entities, relations
