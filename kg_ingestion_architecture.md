# Multi-Modal Knowledge Graph Ingestion System — Architecture Design Plan

*Draft v0.1 — design exploration. Options are enumerated deliberately; nothing here is a locked decision.*

---

## 1. Problem Statement

Build a system that ingests heterogeneous, multi-modal documents (PDF, Word, Markdown, BPMN, images, transcripts, etc.) and incrementally constructs and maintains a **canonical knowledge graph** through inference.

The system must:

1. **Extract** entities and relationships from each source with no fixed, hand-authored schema required up front (schema may be seeded, induced, or both).
2. **Correlate** newly inferred knowledge against everything already in the graph — recognizing when a new entity *is* an existing entity, when a new fact *reinforces*, *extends*, or *contradicts* a stored fact.
3. **Adjust** the existing graph as a consequence — not append-only. Correlation can trigger node merges, property updates, edge invalidation, or restructuring.
4. **Gate every mutation behind human review.** No inferred knowledge enters the canonical graph until a human approves it (subject to an explicit auto-approval policy for high-confidence cases).
5. Preserve **full provenance and lineage** — every node/edge is traceable to its source document, chunk, extraction run, and the reviewer decision that admitted it.

### Core invariant

> The canonical graph is only ever mutated by an approved **commit**. All inference happens in a **staging** layer. The HITL gate is the promotion boundary between staging and canonical.

This single invariant is what makes the system trustworthy, auditable, and safe to run continuously. Most of the architecture exists to serve it.

### Non-goals (for v1)

- Real-time / sub-second ingestion. This is a batch-and-review system; latency budget is generous.
- Fully autonomous operation. Human review is a feature, not a limitation to engineer away — though the *volume* of review is aggressively minimized.
- Being a general-purpose graph database. We build *on top of* one.

### Functional requirements

| # | Requirement |
|---|-------------|
| F1 | Accept ≥4 input modalities via a pluggable parser interface |
| F2 | Extract typed entities + typed relationships + supporting evidence spans |
| F3 | Resolve new entities against the canonical graph (entity resolution) |
| F4 | Detect and surface contradictions with existing facts |
| F5 | Score confidence on every candidate node/edge |
| F6 | Route candidates to auto-approve / human-review / auto-reject by policy |
| F7 | Present a diff (proposed change vs. current canonical subgraph) for review |
| F8 | Commit approved changes idempotently with rollback capability |
| F9 | Record provenance for every element and every decision |
| F10 | Serve downstream retrieval (graph + vector) over the canonical graph |

### Non-functional requirements

- **Idempotency** — re-ingesting the same document produces no duplicate knowledge.
- **Auditability** — every canonical fact answers "where did this come from and who approved it?"
- **Incrementality** — adding a document never requires reprocessing the corpus.
- **Durability** — pipeline runs span human wait states (hours/days); execution state must survive restarts.
- **Cost observability** — extraction is the dominant token cost; it must be measurable and cappable.

---

## 2. System Components (logical view)

```
                          ┌─────────────────────────────────────────────┐
   sources                │              CANONICAL GRAPH                 │
  ┌────────┐              │        (graph DB + vector index)             │
  │pdf docx│              │        the trusted, queryable KG             │
  │md bpmn │              └───────────────▲──────────────┬──────────────┘
  │img txt │                              │ commit        │ read
  └───┬────┘                     ┌────────┴───────┐       │
      │                          │ COMMIT / MERGE │       │
      ▼                          │    ENGINE      │       ▼
┌───────────┐                    └────────▲───────┘  ┌─────────┐
│ INGESTION │                             │ approve  │RETRIEVAL│
│  & NORM   │                    ┌────────┴───────┐  │  LAYER  │
│ (parsers) │                    │  HITL REVIEW   │  └─────────┘
└─────┬─────┘                    │  + ROUTING     │
      ▼                          └────────▲───────┘
┌───────────┐                             │ candidates + confidence
│DECOMPOSE  │                    ┌─────────┴──────┐
│ (chunk /  │                    │ CONFIDENCE &   │
│  atomize) │                    │ CONFLICT ENGINE│
└─────┬─────┘                    └────────▲───────┘
      ▼                                   │ resolved candidates
┌───────────┐        ┌──────────┐  ┌──────┴───────────┐
│EXTRACTION │───────▶│ STAGING  │─▶│ ENTITY RESOLUTION│
│ (triples) │        │  GRAPH   │  │  & CORRELATION   │
└─────┬─────┘        └──────────┘  └──────────────────┘
      │  ▲                                   ▲
      ▼  │ schema                            │ query canonical
┌───────────┐                                │
│ SCHEMA /  │◀───────────────────────────────┘
│ ONTOLOGY  │
│  MANAGER  │
└───────────┘

   ORCHESTRATION (LangGraph / durable runtime) wraps all of the above
   PROVENANCE / LINEAGE store spans staging + canonical
   EVALUATION + FEEDBACK loop observes commits and reviewer decisions
```

**Component responsibilities:**

- **Ingestion & Normalization** — modality-specific parsers → a common normalized document representation (text + layout + structural hints + extracted media).
- **Decomposition** — split normalized docs into extraction units (chunks / propositions / atomic facts). For BPMN, this is structural parsing, not chunking.
- **Extraction Engine** — turn units into candidate `(subject, predicate, object)` triples (or n-tuples) with evidence spans and per-element extraction confidence.
- **Schema / Ontology Manager** — governs entity/relation types: fixed, induced, or hybrid. Approves new types when discovered.
- **Staging Graph** — quarantine for all candidates before they're trusted. Never queried by downstream consumers.
- **Entity Resolution & Correlation** — match staged entities against canonical entities; identify reinforcing / extending / contradicting relationships.
- **Confidence & Conflict Engine** — combine signals into a decision-grade score; detect and characterize contradictions; apply temporal reconciliation.
- **HITL Review + Routing** — apply the auto/review/reject policy; present diffs; capture accept/edit/reject/merge/split decisions.
- **Commit / Merge Engine** — apply approved changes to canonical as an idempotent, transactional patch; write provenance.
- **Provenance / Lineage Store** — element → chunk → document lineage plus extraction-run and reviewer-decision metadata.
- **Retrieval Layer** — serve queries over canonical (graph traversal + vector + hybrid).
- **Orchestration** — durable workflow engine coordinating the whole pipeline across human wait states.
- **Evaluation & Feedback** — measure ingestion quality; feed reviewer decisions back into extraction/resolution (active learning).

---

## 3. End-to-End Process Flow

### 3.1 Ingest path (new document → canonical graph)

1. **Receive & register** — document lands, gets a stable ID, hashed for dedup, provenance record opened.
2. **Parse & normalize** — modality parser emits normalized representation. BPMN yields ground-truth structure directly.
3. **Decompose** — split into extraction units; carry layout/structural context forward.
4. **Extract** — LLM (or hybrid) emits candidate triples + evidence spans + extraction confidence, schema-guided by the Ontology Manager's current view.
5. **Land in staging** — candidates written to the staging graph, tagged with source lineage. *Nothing touches canonical yet.*
6. **Resolve & correlate** — for each staged entity: block → match → cluster against canonical. For each staged relation: check whether it reinforces, extends, or contradicts existing edges. Produce a **proposed patch** (creates / merges / updates / invalidations) against canonical.
7. **Score & reconcile** — Confidence & Conflict Engine assigns a decision score to each patch element, resolves temporal conflicts (valid-time), flags contradictions.
8. **Route** — policy sends each patch element to: **auto-approve**, **human review**, or **auto-reject**.
9. **HITL gate** — reviewer sees the proposed patch as a diff against the current canonical subgraph; accepts / edits / rejects / forces merge or split. Decisions logged. *(This is the `interrupt()` boundary.)*
10. **Commit** — approved patch applied to canonical idempotently and transactionally; provenance edges written; vector index updated.
11. **Feedback** — reviewer decisions and commit outcomes feed the evaluation store and active-learning selector.

### 3.2 Adjust path (why this is not append-only)

Correlation in step 6 can conclude that a new fact relates to existing knowledge in ways that require *mutating* canonical, not just adding:

| Correlation outcome | Adjustment to canonical |
|---|---|
| New entity **is** an existing entity | Merge nodes; consolidate edges/properties; add `SAME_AS` provenance |
| New fact **reinforces** an existing edge | Increase support/confidence; add another source edge |
| New fact **extends** an entity | Add properties / new edges to the existing node |
| New fact **contradicts** an existing edge | Temporal reconciliation: invalidate old edge (set `valid_to`), assert new one, OR surface both to reviewer for adjudication |
| New fact **refines granularity** | Split an over-merged node, or re-parent under a more specific type |

Every one of these is a reviewable patch. The "adjust" requirement is precisely what makes the commit engine a **diff/patch applier**, not an inserter.

### 3.3 Self-correcting loop (optional, mirrors your T&C→DMN pattern)

Extraction and resolution share blind spots (correlated errors). A structural-check node after resolution can catch orphan entities, dangling relations, and type violations, and route back to re-extract with a corrective prompt before anything reaches a human — reducing review burden and keeping humans focused on genuine ambiguity.

---

## 4. Options at Each Level

Each layer lists valid options with tradeoffs and a "pick when" heuristic. Layers are largely independent — you can mix.

### L0 · Ingestion & Parsing

| Option | What | Pros | Cons | Pick when |
|---|---|---|---|---|
| Native libs (PyMuPDF, python-docx, markdown-it) | Direct format parsing | Fast, free, deterministic | Weak on complex layout, no OCR | Clean digital docs |
| Docling / Unstructured.io | Layout-aware document parsers | Handles tables, reading order, many formats | Heavier deps, variable quality | Mixed real-world corpora |
| LlamaParse / Marker | LLM/ML-assisted parsing | Strong on messy PDFs, tables | Cost, API dependency | Scanned/complex PDFs |
| Vision-LLM parse | Feed page images to a multimodal model | Best on diagrams, forms, layout | Highest cost, hallucination risk | Diagram-heavy or scanned |
| **BPMN native XML parse** | Parse `.bpmn` XML directly | **Ground-truth structure, no inference needed** | Format-specific | Always, for BPMN |
| OCR (Tesseract / cloud DI) | Text from images | Enables scanned docs | Error-prone, needs cleanup | Scanned inputs |

> **Key insight:** BPMN is your *easiest* modality — it's a typed graph already. Parse it as structural ground truth and let extraction merely enrich it.

### L1 · Decomposition / Chunking

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| Fixed-size + overlap | Trivial, cheap | Splits entities across chunks | Baseline only |
| Semantic chunking | Respects meaning boundaries | Extra embedding pass | General prose |
| Proposition / atomic-fact decomposition | Highest extraction precision & stability (see ATOM) | More LLM calls | Quality-critical KG |
| Layout/hierarchy-aware (parent-child) | Preserves doc structure | Parser-dependent | Structured docs |
| Structural (BPMN) | Exact | Format-specific | BPMN/graph inputs |

### L2 · Extraction Engine

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| LLM open triple extraction (GraphRAG/LightRAG style) | Zero-shot, flexible, rich | Token cost, schema drift, noise | Exploratory / broad domains |
| Schema-guided LLM (iText2KG JSON schema) | Cleaner, higher signal-to-noise | Needs a schema | Known-ish domain |
| Single-shot n-tuple (ATOM) | Fewer LLM calls, parallel | Newer, less tooling | Latency/cost sensitive |
| Separate entity → relation (iText2KG/Graphiti) | Modular, controllable | ~2× LLM calls | Precision over speed |
| Fine-tuned extractor (REBEL, GLiNER, SpanMarker) | Cheap at scale, fast | Training effort, domain-bound | >~1,500 docs/month |
| Dependency-parse / SpaCy noun-phrase | Very cheap, ~94% of LLM quality (per 2507.03226) | Lower ceiling, English-centric | Cost-dominated scale |
| Multi-agent (AutoKG) | Coordinated extraction+reasoning | Complex, costly | Hard reasoning domains |

**Structured-output enforcement** (orthogonal, use with any LLM option): function calling / JSON mode, **Instructor**, **Outlines**, **BAML**, or **DSPy** (adds optimization). Recommend one of these for reliability — free-text triple parsing is fragile.

### L3 · Schema / Ontology Strategy

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| Fixed ontology (OWL/RDFS/SHACL, LinkML) | Consistent, validatable, high precision | Rigid, needs upfront design | Well-understood domain |
| Schema-free / open | Max recall, zero setup | Drift, fragmentation, noise | Pure discovery |
| Dynamic induction (AutoSchemaKG, Tree-KG) | Auto-discovers types from corpus | Instability, needs curation | Unknown/evolving domain |
| **Hybrid: seed + gated extension** | Stable core + controlled growth; **new types themselves go through HITL** | Governance overhead | **Recommended default** |

**Validation formalism:** SHACL shapes or ontology checks as a hard gate before staging→review (structural validity, type constraints, cardinality).

### L4 · Staging Store

| Option | Pros | Cons |
|---|---|---|
| Separate graph namespace/label set in same DB | Simple, one system | Risk of accidental cross-read |
| Separate graph database instance | Hard isolation | Ops overhead |
| Property-flag on nodes (`status: staged/committed`) | Cheapest | Query discipline required, leak risk |
| Event log of candidate patches (Kafka/Redis stream) | Replayable, decoupled, natural audit trail | More moving parts |

> Recommend **event log of proposed patches** + a lightweight staging graph. The log doubles as an audit trail and enables replay/reprocessing — and fits your Kafka/Redis background.

### L5 · Entity Resolution & Correlation *(the hard core)*

**Blocking** (reduce n² comparisons): exact-key · embedding ANN (HNSW) · LSH · phonetic (Soundex/Metaphone) · n-gram/q-gram.

**Matching:**

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| Deterministic rules | Fast, explainable, free | Brittle on variation | Clear IDs exist |
| Embedding cosine + threshold (iText2KG) | Cheap, parallel, LLM-free | Threshold tuning, misses hard cases | Scale, speed |
| ML pairwise (Splink, Zingg, Dedupe.io) | Calibrated probabilities | Training/labels | 1M–100M entities |
| Cross-encoder | High accuracy on pairs | Slower | Precision on candidates |
| LLM pairwise + CoT | Best on ambiguous cases, explains itself | Expensive, latency | The uncertain margin only |
| **Cascade: rules → ML → LLM** | **Balances cost/latency/accuracy; ~40% by rules, escalate the rest** | Orchestration | **Recommended default** |

**Clustering** (group matched pairs into one resolved entity): connected components (simple, over-merges) · **Louvain** (cuts weak edges, avoids over-merge) · correlation clustering · hierarchical. Emit `SAME_AS` edges, then merge components.

**Tools:** Neo4j GDS (node similarity, KNN, community detection) · Splink · Zingg · Dedupe.io · Senzing (commercial).

> **Warnings baked in:** naive string dedup misses synonyms/abbreviations/typos/multilingual; pure transitive closure over-merges — always cut weak edges (Louvain) before merging.

### L6 · Conflict & Temporal Reconciliation

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| Bi-temporal edges (valid-time + transaction-time, à la Graphiti) | Full history, clean contradiction handling | Model complexity | Facts change over time |
| Edge invalidation (soft delete + `valid_to`) | Preserves history | Query must filter | Contradictions common |
| Node/edge versioning | Complete audit | Storage growth | Compliance-heavy |
| Source-authority / recency policy | Automatic resolution | Can be wrong | Trusted source hierarchy |
| Provenance-weighted voting (SCICERO support-level) | Robust to single bad source | Needs multi-source | Redundant corpora |
| LLM adjudication → HITL | Handles nuance | Cost, still needs human | Genuine semantic conflicts |

> Note: iText2KG's dynamic mode explicitly does **not** handle temporal/logical conflict resolution — this layer is where you add real value and where genuine research difficulty lives.

### L7 · Confidence Scoring

Signals to combine per candidate: extraction confidence (logprobs / self-consistency across samples) · **source support count** (how many sources assert it — SCICERO) · resolution match score · ontology/SHACL validation pass · cross-encoder agreement.

| Combination | Pros | Cons |
|---|---|---|
| Weighted linear blend | Simple, tunable | Weights are guesses |
| Learned classifier | Calibrated | Needs labeled decisions (bootstrap from HITL) |
| Rule tiers | Transparent | Coarse |

> Store the final confidence **as a queryable graph attribute** so downstream retrieval and audits can filter on it.

### L8 · Routing / HITL Policy

| Option | Pros | Cons |
|---|---|---|
| Static thresholds (auto ≥ hi, reject < lo, else review) | Simple, predictable | Needs calibration |
| Active learning selection (uncertainty / expected-model-change) | Minimizes review burden, maximizes learning | Extra machinery |
| Always-review for sensitive types/edges | Safe for high-stakes | More review |
| Sampling audit of auto-approved | Catches silent drift | Adds review even to "safe" path |
| LLM-as-judge pre-filter | Cuts obvious junk before humans | Adds cost, can err |

> Recommend static thresholds **v1**, add active-learning selection **v2** once you have labeled reviewer decisions. Always-review new schema types regardless of score.

### L9 · HITL Review Interface

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| Custom Next.js + AG-UI/A2UI (your stack) | Tailored diff/merge UX, streaming | Build effort | You want it right |
| Argilla / Label Studio | Off-the-shelf annotation | Not graph-native, awkward for merges | Fast start |
| Neo4j Bloom / graph viz | Visual subgraph review | Not a decision workflow | Visual inspection |
| ORKG-style curator UI | Proven for scholarly KG curation | Domain-shaped | Reference design |

**Review actions to support:** accept · edit (fix type/label/props) · reject · force-merge · force-split · defer. **Presentation:** diff of proposed patch vs. current canonical subgraph; comparative (A/B) review reduces cognitive load vs. absolute scoring; batch similar candidates. **Mechanism:** LangGraph `interrupt()` → surface patch → `Command(resume=decision)`.

### L10 · Commit / Merge Engine

| Option | Pros | Cons |
|---|---|---|
| Idempotent upsert (Cypher `MERGE`) | Safe re-runs, dedup | Must key correctly |
| Diff/patch applier | Handles adjust-path (merge/update/invalidate) | More logic |
| APOC / GDS merge utilities | Battle-tested node merge | Neo4j-specific |
| Transactional write + rollback | Atomicity, safety | Perf overhead |

> This engine must be a **patch applier**, not an inserter (see §3.2). Always write provenance edges in the same transaction as the fact.

### L11 · Provenance & Lineage

- **Model:** W3C **PROV-O** or a lightweight custom `(:Entity)-[:EXTRACTED_FROM]->(:Chunk)-[:PART_OF]->(:Document)` plus `(:ExtractionRun)` and `(:ReviewDecision)` nodes.
- Capture: source doc + hash, chunk/span, extraction run + model + prompt version, confidence, reviewer + decision + timestamp.
- Enables the audit answer: *"where did this fact come from and who approved it?"*

### L12 · Storage Backends

**Graph:** Neo4j (mature, GDS, vector index) · Memgraph (in-mem, fast) · Kuzu (embedded) · Neptune (managed) · Apache AGE (Postgres extension) · FalkorDB · ArangoDB.

**Vector:** Qdrant · pgvector · Weaviate · Milvus · LanceDB · **or Neo4j's native vector index** (one system for both).

| Topology | Pros | Cons |
|---|---|---|
| Separate graph + vector (Neo4j + Qdrant) | Best-of-breed each | Two systems, sync |
| Unified (Neo4j vector index) | One system, simpler ops | Vector features less rich |
| Postgres-centric (AGE + pgvector) | Single DB, transactional | Graph perf ceiling |

> Given your existing Qdrant + Neo4j + Docker/OrbStack setup, **Neo4j (canonical + GDS resolution) + Qdrant (extraction/dedup embeddings)** is the natural default; consider Neo4j's own vector index to collapse to one system if ops simplicity wins.

### L13 · Retrieval Layer (downstream)

Local search (entity neighborhood) · global search (community summaries, Leiden clustering) · hybrid vector+graph · path-based reasoning (RoG) · subgraph retrieval (SubgraphRAG) · Text2Cypher (NL→query). Add a cross-encoder reranker (Cohere / ms-marco-MiniLM) — same pattern as your Airbnb project.

### L14 · Orchestration

| Option | Pros | Cons | Pick when |
|---|---|---|---|
| **LangGraph** (your default) | Native `interrupt()`/`Command(resume=)`, `Send` fan-out, checkpointing | Checkpoint durability needs care | HITL-centric graphs |
| Temporal | True durable execution across day-long human waits; retries/timeouts | Heavier, separate infra | Long-lived, mission-critical |
| Prefect / Dagster / Airflow | Mature batch orchestration | Not HITL- or agent-shaped | Scheduled batch ingest |
| Plain async + queue | Full control | Reinvent durability | Small scale |

> HITL means runs pause for hours/days — you need **durable execution**. LangGraph with a Postgres checkpointer covers it; Temporal if this becomes production-critical (and it maps directly onto your "Durable Workflows" work).

### L15 · Evaluation & Feedback

- **Ingestion quality:** RAGAS faithfulness (does the graph fact match the source span?), triple-level precision/recall vs. a gold set, ontology-conformance rate.
- **Review economics:** auto-approve accuracy (sampled), human-agreement rate, review time per candidate, % escalated.
- **Feedback loop:** reviewer accept/reject/edit decisions → active-learning selector (L8) + few-shot/fine-tune data for extraction (L2) and match examples for resolution (L5).

---

## 5. Recommended Default Stack (opinionated, given your environment)

A concrete starting point — every choice is swappable per §4.

- **Parsing:** Docling for docs + native BPMN XML parser + vision-LLM fallback for scans.
- **Decomposition:** atomic-fact for prose; structural for BPMN.
- **Extraction:** open triple extraction with Instructor/BAML structured output, guided by the *current induced schema* (soft constraint, not hard filter — new types must be able to surface); DSPy later for optimization. *(Open-domain: full schema-guided extraction is off the table.)*
- **Schema:** thin upper-level seed (Entity/Event/Agent/Artifact-tier) + dynamic induction; **every induced type is itself a HITL-reviewed candidate** before it can constrain extraction; SHACL validation on the stable core. Periodic curation pass to merge near-duplicate induced types.
- **Staging:** Kafka/Redis patch-event log + Neo4j staging labels.
- **Resolution:** cascade (rules → embedding cosine → LLM), Neo4j GDS for clustering (Louvain). *(Open-domain: the rules tier only covers explicit identifiers — URLs, emails, doc-internal IDs — so expect the embedding and LLM tiers to carry most of the load; budget accordingly.)*
- **Conflict:** bi-temporal edges; temporal contradictions auto-reconcile (invalidate + assert), semantic conflicts always escalate to HITL (per §7). Provenance-weighted voting as a confidence signal, not an auto-resolver.
- **Confidence:** weighted blend v1 → learned classifier v2 (bootstrapped from reviews).
- **Routing:** static thresholds v1 → active learning v2; always-review new types.
- **HITL UI:** custom Next.js + AG-UI/A2UI, diff-of-subgraph view, `interrupt()`/`Command(resume=)`.
- **Commit:** Cypher `MERGE` patch applier, transactional, provenance in same txn; **rebase check at commit time** — re-validate the patch's preconditions against current canonical, re-diff + re-queue on mismatch (per §7). Merges keep source nodes + `SAME_AS` edges for reversibility.
- **Storage:** Neo4j (canonical + GDS) + Qdrant (embeddings).
- **Retrieval:** hybrid local/global + cross-encoder rerank.
- **Orchestration:** LangGraph + Postgres checkpointer (Temporal if it goes production-critical).
- **Eval:** RAGAS faithfulness + triple P/R + review-economics dashboard.

---

## 6. Hardest Problems (flagged for deliberate design)

1. **Entity resolution at the margins** — the ambiguous 5–10% that rules and embeddings can't settle. This is where LLM matching + HITL earn their cost.
2. **Temporal / logical conflict resolution** — new data contradicting old. Genuinely unsolved in most OSS (iText2KG punts on it). Bi-temporal modeling is the foundation; adjudication policy is the art.
3. **Schema drift** — free inference fragments the graph over time. Gated type extension + periodic curation is the containment strategy.
4. **Over-merging** — collapsing distinct entities is worse than duplicates because it's hard to detect after the fact. Louvain edge-cutting + reversible merges (keep `SAME_AS`, don't destroy source nodes) mitigate.
5. **Review-burden economics** — the system is only viable if humans review a small, high-value fraction. Active learning + confidence routing + self-correcting pre-filter are the levers.

---

## 7. Resolved Decisions (2026-07-08)

The five pre-build questions have been decided:

| Question | Decision | Consequence |
|---|---|---|
| **Domain scope** | **Open-domain** | No real seed domain ontology is possible. Schema strategy shifts to a thin upper-level seed + dynamic induction (AutoSchemaKG-style), with every induced type HITL-gated. Extraction shifts to open triple extraction with induced-schema guidance. Entity resolution loses its deterministic-rules tier for most entities; over-merging and schema drift become the top two risks (§6). |
| **Contradiction policy** | **Tiered** | Temporal contradictions (same sources, value changed over time) auto-reconcile bi-temporally: invalidate old edge (`valid_to`), assert new. Genuine semantic conflicts (same validity window, incompatible claims) always escalate to review. |
| **Reversibility** | **Reversible merges, forward-fix the rest** | Merges never destroy source nodes — keep `SAME_AS` provenance so any merge can be unwound (the one mistake that's hard to detect after the fact). All other errors are corrected by new reviewed patches; no general rollback machinery. |
| **Review SLA** | **No expiry, rebase at commit** | Pending patches never expire. At approval time the commit engine re-validates the patch against current canonical; if the underlying subgraph changed since the diff was produced, the item is re-diffed and re-queued rather than applied blind. |
| **Reviewers / tenancy** | **Single curator (v1)** | One graph, one reviewer. No assignment/conflict logic, no tenant isolation. `ReviewDecision` provenance nodes carry a reviewer identity field so multi-reviewer can be added later without migration. |

---

## References (design inputs)

- Edge et al., *GraphRAG* (2024) — community-summary construction.
- Guo et al., *LightRAG* (2024) — lightweight dual-level retrieval.
- Lairgi et al., *iText2KG* (WISE 2024, arXiv 2409.03284) — incremental construction, cosine resolution.
- *ATOM* (arXiv 2510.22590) — parallel n-tuple extraction, LLM-free merge.
- Rasmussen et al., *Graphiti / Zep* (2025) — bi-temporal agent memory graph.
- *Cognee* (topoteretes/cognee) — OSS multi-format KG memory pipeline.
- Bai et al., *AutoSchemaKG* (arXiv 2505.23628) — dynamic schema induction.
- *SCICERO* & "Knowledge graph validation by integrating LLMs and human-in-the-loop" (ScienceDirect 2025) — support-level confidence + HITL validation.
- "Less is More: Denoising Knowledge Graphs for RAG" (arXiv 2510.14271) — entity resolution as quality control.
- "LLM-empowered knowledge graph construction: A survey" (arXiv 2510.20345) — field overview.
- Neo4j LLM Knowledge Graph Builder — reference implementation.
