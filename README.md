# kgi — Knowledge Graph Ingestion with HITL Gating

Multi-modal document ingestion that incrementally builds a canonical knowledge graph.
All inference lands in staging; **the canonical graph is only mutated by approved
commits** — the human review gate is the promotion boundary.

Design doc: [`kg_ingestion_architecture.md`](kg_ingestion_architecture.md) (§7 has the
resolved pre-build decisions: open-domain, tiered contradiction policy, reversible
merges, rebase-at-commit, single curator).
**Lifecycle documentation with diagrams: [`docs/lifecycle.md`](docs/lifecycle.md)** —
how a document becomes trusted knowledge, the patch state machine, the durable review
flow, and bi-temporal fact evolution.

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

The **resolution cascade is live** (rules → embedding → LLM), with thresholds calibrated
empirically on bge-small-en-v1.5: true aliases ("Acme Corp"/"Acme Corporation", 0.99)
auto-merge with reviewable `SAME_AS` provenance, while lookalike siblings
("GripperOne"/"GripperTwo", 0.87) land in the ambiguous band and go to the LLM judge —
which keeps them distinct. Predicate paraphrases ("is chief executive officer of" ≈
"is CEO of") normalize to the known predicate and reinforce instead of duplicating.
Entity/predicate vectors (Qdrant, local fastembed embeddings) are indexed at commit
time only — staged candidates are never searchable.

The **conflict engine implements the §7 tiered contradiction policy**: a staged
relation whose canonical subject already has a same-predicate edge to a different
object is LLM-adjudicated (conservative prompt — unclear evidence defaults to
semantic conflict, because a wrong supersession silently rewrites history). Temporal
supersessions auto-build `InvalidateEdge(valid_to) + AssertEdge` (bi-temporal, history
preserved); semantic conflicts force-route both ops to human review. Extraction is
guided by canonical's live vocabulary (types + predicates), so repeat documents reuse
terms instead of coining paraphrases.

**The review loop is usable from the terminal**: `kgi ingest <doc>` parks at the gate
and exits; `kgi pending` lists parked patches; `kgi review <patch_id>` walks each op
(accept / reject-with-note / defer) and resumes the run — possibly days later, from a
different process, thanks to the Postgres checkpointer. Every decision is recorded as
`ReviewDecision` provenance with reviewer identity (the future active-learning labels).
`--all accept|reject` for batch decisions.

**Retrieval (L13) is live, local-search style**: `kgi search "<query>"` anchors on
entities by vector similarity and expands their neighborhood; `kgi ask "<question>"`
answers grounded ONLY in reviewed facts, citing each fact and its source documents.
`--as-of DATE` time-travels over the bi-temporal edges — "Where is Acme headquartered?"
answers Zurich today and Basel `--as-of 2024-06-01`, each cited to its sources.
Questions the graph can't answer get an honest refusal, not a hallucination.
`kgi serve` runs the graph viewer (force-directed canvas + provenance side panel +
review queue) at localhost:8100.

Remaining, in suggested order:

1. Eval harness (L15): faithfulness vs source spans, review-economics metrics
2. Viewer: search/ask UI + accept/reject from the browser (grow into the L9 client)
3. Schema governance (deferred until type count or reviewer count demands it)
```
