"""Decomposition (L1): normalized doc -> extraction units.

v1: block-per-unit for prose (upgrade path: LLM atomic-fact decomposition per ATOM);
structural passthrough for BPMN, whose ground-truth triples skip extraction anyway.
"""

from kgi.models import ExtractionUnit, NormalizedDocument


def decompose(ndoc: NormalizedDocument) -> list[ExtractionUnit]:
    units = []
    for block in ndoc.blocks:
        if block.kind == "heading" or not block.text.strip():
            continue  # headings travel as context, not as units
        units.append(
            ExtractionUnit(
                unit_id=f"{block.block_id}_u0",
                doc_id=ndoc.doc.doc_id,
                block_ids=[block.block_id],
                text=block.text,
                context=block.context,
            )
        )
    return units
