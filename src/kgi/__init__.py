"""kgi — multi-modal knowledge graph ingestion with HITL-gated inference.

Layer map (see kg_ingestion_architecture.md):
  ingestion (L0) -> decomposition (L1) -> extraction (L2) -> staging (L4)
  -> resolution (L5) -> conflict (L6) -> confidence (L7) -> routing (L8)
  -> review (L9) -> commit (L10). schema (L3) and stores (L12) cross-cut.
  pipeline/ wires it all as a LangGraph (L14).
"""

__version__ = "0.1.0"
