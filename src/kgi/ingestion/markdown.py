"""Minimal Markdown parser (L0): heading-aware blocks with parent-heading context."""

from pathlib import Path

from kgi.ingestion.base import register_parser
from kgi.models import Document, Modality, NormalizedDocument
from kgi.models.document import Block


class MarkdownParser:
    modality = Modality.markdown

    def parse(self, doc: Document, path: Path) -> NormalizedDocument:
        blocks: list[Block] = []
        heading_stack: list[str] = []
        for i, raw in enumerate(path.read_text().split("\n\n")):
            text = raw.strip()
            if not text:
                continue
            if text.startswith("#"):
                level = len(text) - len(text.lstrip("#"))
                heading_stack = heading_stack[: level - 1] + [text.lstrip("# ").strip()]
                kind = "heading"
            else:
                kind = "paragraph"
            blocks.append(
                Block(
                    block_id=f"{doc.doc_id}_b{i}",
                    kind=kind,
                    text=text,
                    context={"headings": list(heading_stack)},
                )
            )
        return NormalizedDocument(doc=doc, blocks=blocks)


register_parser(MarkdownParser())
