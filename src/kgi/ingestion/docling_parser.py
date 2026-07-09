"""Docling-based parser for PDF/DOCX (L0). Optional extra: pip install kgi[parsers]."""

from pathlib import Path

from docling.document_converter import DocumentConverter

from kgi.ingestion.base import register_parser
from kgi.models import Document, Modality, NormalizedDocument
from kgi.models.document import Block


class DoclingParser:
    def __init__(self, modality: Modality):
        self.modality = modality
        self._converter = DocumentConverter()

    def parse(self, doc: Document, path: Path) -> NormalizedDocument:
        result = self._converter.convert(str(path))
        blocks = [
            Block(
                block_id=f"{doc.doc_id}_b{i}",
                kind=item.label if hasattr(item, "label") else "paragraph",
                text=item.text,
                context={},
            )
            for i, (item, _level) in enumerate(result.document.iterate_items())
            if getattr(item, "text", "").strip()
        ]
        return NormalizedDocument(doc=doc, blocks=blocks)


register_parser(DoclingParser(Modality.pdf))
register_parser(DoclingParser(Modality.docx))
