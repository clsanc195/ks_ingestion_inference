"""Candidate knowledge emitted by extraction (L2), before resolution. Lives in staging only."""

from pydantic import BaseModel, Field


class EvidenceSpan(BaseModel):
    """Where in the source a candidate is asserted. Required on every candidate (F2, F9)."""

    doc_id: str
    unit_id: str
    quote: str  # verbatim supporting text


class CandidateEntity(BaseModel):
    temp_id: str  # staging-local id; resolution maps it to a canonical id or a CreateNode
    name: str
    # Open-domain: type is a *proposal* against the induced schema; unknown types are
    # allowed here and become schema-extension candidates (L3), themselves HITL-gated.
    entity_type: str
    properties: dict = Field(default_factory=dict)
    evidence: list[EvidenceSpan]
    extraction_confidence: float = Field(ge=0.0, le=1.0)


class CandidateRelation(BaseModel):
    temp_id: str
    subject_temp_id: str
    predicate: str
    object_temp_id: str
    properties: dict = Field(default_factory=dict)
    # Valid-time as asserted by the source, if stated (feeds bi-temporal reconciliation, L6)
    valid_from: str | None = None
    valid_to: str | None = None
    evidence: list[EvidenceSpan]
    extraction_confidence: float = Field(ge=0.0, le=1.0)
