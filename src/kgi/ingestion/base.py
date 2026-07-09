"""Pluggable parser interface (L0, F1). One parser per modality, registered at import."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from kgi.models import Document, Modality, NormalizedDocument

_REGISTRY: dict[Modality, "Parser"] = {}


class Parser(Protocol):
    modality: Modality

    def parse(self, doc: Document, path: Path) -> NormalizedDocument: ...


def register_parser(parser: Parser) -> None:
    _REGISTRY[parser.modality] = parser


def get_parser(modality: Modality) -> Parser:
    if modality not in _REGISTRY:
        raise KeyError(f"no parser registered for modality {modality.value!r}")
    return _REGISTRY[modality]


def register_document(path: Path, modality: Modality) -> Document:
    """Receive & register (§3.1 step 1): stable id via content hash → idempotent re-ingest."""
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return Document(
        doc_id=f"doc_{content_hash[:16]}",
        modality=modality,
        source_uri=str(path.resolve()),
        content_hash=content_hash,
        registered_at=datetime.now(timezone.utc),
    )
