"""Normalized document representation shared by all parsers (L0) and decomposition (L1)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Modality(str, Enum):
    pdf = "pdf"
    docx = "docx"
    markdown = "markdown"
    bpmn = "bpmn"
    image = "image"
    transcript = "transcript"


class Document(BaseModel):
    """A registered source document. `doc_id` is stable; `content_hash` drives idempotency."""

    doc_id: str
    modality: Modality
    source_uri: str
    content_hash: str
    registered_at: datetime
    metadata: dict = Field(default_factory=dict)


class Block(BaseModel):
    """One structural block of a normalized document (paragraph, table, BPMN element, ...)."""

    block_id: str
    kind: str  # paragraph | heading | table | figure | bpmn_task | bpmn_gateway | utterance | ...
    text: str
    # Structural hints carried forward to extraction (page, bbox, parent heading, lane, ...)
    context: dict = Field(default_factory=dict)


class NormalizedDocument(BaseModel):
    doc: Document
    blocks: list[Block]
    # BPMN and other structured formats emit ground-truth triples directly at parse
    # time; these bypass LLM extraction and enter staging as high-confidence candidates.
    ground_truth_triples: list[dict] = Field(default_factory=list)


class ExtractionUnit(BaseModel):
    """One unit sent to the extractor: an atomic fact / proposition / structural element."""

    unit_id: str
    doc_id: str
    block_ids: list[str]
    text: str
    context: dict = Field(default_factory=dict)
