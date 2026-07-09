# kgi — Knowledge Graph Ingestion with HITL Gating

Multi-modal document ingestion that incrementally builds a canonical knowledge graph.
All inference lands in staging; **the canonical graph is only mutated by approved
commits** — the human review gate is the promotion boundary.

Design doc: [`kg_ingestion_architecture.md`](kg_ingestion_architecture.md) (§7 has the
resolved pre-build decisions: open-domain, tiered contradiction policy, reversible
merges, rebase-at-commit, single curator).

## Layout

```
src/kgi/
  models/         # Document/candidate/patch/provenance models — Patch is the core
  ingestion/      # L0  pluggable parsers (markdown, BPMN ground-truth, docling)
  decomposition/  # L1  doc -> extraction units
  extraction/     # L2  open triple extraction (Instructor + Claude)
  schema/         # L3  thin upper ontology + HITL-gated induced types
  resolution/     # L5  cascade: rules -> embedding -> LLM; Louvain clustering
  conflict/       # L6  correlation -> patch ops; bi-temporal reconciliation
  confidence/     # L7  weighted blend scoring
  routing/        # L8  thresholds + force-review overrides
  commit/         # L10 patch applier: idempotent, rebase-checked, reversible merges
  stores/         # L12 Neo4j (canonical+staging) and Qdrant (entity embeddings)
  pipeline/       # L14 LangGraph wiring with interrupt() review gate
  cli.py
```

## Quickstart

```bash
docker compose up -d          # Neo4j (7474/7687), Qdrant (6333), Postgres (5432)
cp .env.example .env          # add ANTHROPIC_API_KEY
uv sync --extra dev           # or: pip install -e ".[dev]"
uv run pytest                 # patch-model smoke tests
uv run kgi ingest path/to/doc.md
```

## Status

**The pipeline runs end-to-end**: parse → decompose → extract (LLM + BPMN ground truth)
→ rules-tier resolve (intra-batch dedup + exact-name canonical match) → score → route →
stage → `interrupt()` review gate → commit. Verified live against Neo4j + the Anthropic
API: the invariant holds (staged patch, canonical untouched until approval), duplicate
documents short-circuit on content hash, and the commit engine is an idempotent
ledger-backed patch applier with rebase-at-commit precondition checks, dependency
blocking, and reversible merges. Integration tests need a live Neo4j
(`docker compose up -d neo4j`; **they wipe the database**).

Known limitation, by design: resolution is exact-match only, so paraphrases across
*different* documents ("is CEO of" vs "is chief executive officer of") create parallel
edges. That's the next layer's job. Remaining stubs, in suggested build order:

1. `resolution/cascade.py` embedding + LLM tiers (entity *and* predicate paraphrase)
2. `conflict/engine.py` correlation → contradiction detection → bi-temporal ops
3. `cli.py pending`/`review` — terminal review loop before investing in the web UI
```
