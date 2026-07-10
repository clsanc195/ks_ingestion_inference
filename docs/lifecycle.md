# kgi — Application Lifecycle

How a document becomes trusted, queryable knowledge — and why nothing skips the gate.

## The one invariant

> **The canonical graph is only ever mutated by an approved commit.** All inference
> lands in a staging layer; the human review gate is the promotion boundary between
> staging and canonical. Everything else in the system exists to serve this rule.

A consequence worth internalizing: extraction can be wrong, resolution can be wrong,
the conflict adjudicator can be wrong — and none of it matters until a human (or an
explicit auto-approval policy) admits the result. Mistakes are cheap in staging and
reviewable at the boundary.

## Overall lifecycle

```mermaid
flowchart TB
    subgraph sources["📄 Sources"]
        DOC["document<br/>(md · pdf · docx · bpmn)"]
    end

    subgraph pipeline["Ingestion pipeline — LangGraph, durable across restarts"]
        direction TB
        PARSE["<b>parse</b><br/>modality parser → normalized blocks<br/><i>BPMN emits ground-truth triples directly</i>"]
        DUP{{"content hash<br/>seen before?"}}
        DECOMP["<b>decompose</b><br/>blocks → extraction units<br/>(heading context carried along)"]
        EXTRACT["<b>extract</b><br/>LLM → candidate entities + relations<br/>with evidence quotes & confidence<br/><i>guided by canonical's live vocabulary</i>"]
        RESOLVE["<b>resolve</b> — entity resolution cascade<br/>1️⃣ rules: exact name → reuse id<br/>2️⃣ embedding ≥0.93 → MergeInto (SAME_AS)<br/>3️⃣ ambiguous band → LLM judge<br/>no match → CreateNode"]
        CORRELATE["<b>correlate</b> — conflict engine<br/>same fact → ReinforceEdge<br/>new fact → AssertEdge<br/>value changed over time → InvalidateEdge + AssertEdge<br/>incompatible claims → ⚠️ semantic conflict"]
        SCORE["<b>score & route</b><br/>confidence = extraction + support + resolution + validation<br/>≥0.92 auto-approve · <0.30 auto-reject · else review<br/><i>new types & semantic conflicts always review</i>"]
        STAGE["<b>stage</b><br/>patch persisted — canonical untouched"]
    end

    subgraph gate["🚧 HITL gate — run parks here (hours/days)"]
        REVIEW["<b>review</b><br/>terminal: kgi review<br/>browser: queue → accept/reject/defer<br/>every decision → ReviewDecision provenance"]
    end

    subgraph commit["Commit engine — the ONLY writer to canonical"]
        APPLY["<b>commit</b> — per op, one transaction<br/>✓ idempotency ledger (AppliedOp)<br/>✓ rebase check: preconditions re-verified<br/>✓ dependency order; blocked if parent rejected<br/>✓ provenance written with the fact"]
    end

    subgraph canonical["🏛 Canonical knowledge"]
        GRAPH[("Neo4j<br/>entities + bi-temporal facts<br/>+ provenance ledger")]
        VEC[("Qdrant<br/>entity & predicate vectors<br/><i>indexed only after commit</i>")]
    end

    subgraph consumers["Consumers"]
        VIEWER["viewer · localhost:8100<br/>graph canvas + provenance panel"]
        ASK["kgi ask / search<br/>grounded, cited, time-travel QA"]
    end

    DOC --> PARSE --> DUP
    DUP -- "yes — no-op<br/>(idempotency)" --> DONE(("done"))
    DUP -- no --> DECOMP --> EXTRACT --> RESOLVE --> CORRELATE --> SCORE --> STAGE
    STAGE -- "ops needing review" --> REVIEW --> APPLY
    STAGE -- "all auto-approved" --> APPLY
    APPLY --> GRAPH
    APPLY --> VEC
    GRAPH --> VIEWER & ASK
    VEC -. "next document's<br/>resolution & extraction guidance" .-> RESOLVE
```

Two feedback loops are drawn dashed: committed entities/predicates become the
vector index and vocabulary that guide the *next* document's extraction and
resolution — the graph gets better at absorbing documents as it grows.

## Life of a single proposed fact (patch op)

Everything downstream of resolution is a **patch**: a set of typed operations
(`CreateNode`, `MergeInto`, `AssertEdge`, `InvalidateEdge`, …) forming a dependency
DAG. Ops are routed and decided individually, but an op can only commit when its
whole dependency closure is approved — approving an edge whose endpoint was
rejected silently blocks the edge instead of corrupting the graph.

```mermaid
stateDiagram-v2
    [*] --> pending : op created by resolve/correlate
    pending --> approved : reviewer accepts / auto-approve policy
    pending --> rejected : reviewer rejects / auto-reject policy
    pending --> pending : defer (stays in queue)
    approved --> committed : preconditions hold → applied + ledger + provenance
    approved --> requeued : rebase check failed<br/>(canonical changed since diff)
    approved --> blocked : a dependency was rejected/requeued
    rejected --> [*]
    committed --> [*]
    requeued --> [*] : re-enters resolution
    blocked --> [*]
```

The **rebase check** is what makes pending patches safe to leave parked for days:
each op carries preconditions snapshotting the canonical state it was diffed
against (`node_exists`, `edge_absent`, …). At commit time they are re-verified
inside the same transaction as the write; a mismatch re-queues the op instead of
applying it blind.

## The review interaction (durable across processes)

```mermaid
sequenceDiagram
    actor U as curator
    participant CLI as kgi ingest
    participant LG as LangGraph run<br/>(Postgres checkpoint)
    participant ST as staging (Neo4j)
    participant W as viewer / kgi review
    participant CE as commit engine
    participant C as canonical (Neo4j + Qdrant)

    U->>CLI: kgi ingest doc.md
    CLI->>LG: run pipeline
    LG->>ST: stage patch (+ thread_id)
    LG-->>CLI: interrupt() — run parks, process exits
    Note over LG: hours or days pass —<br/>checkpoint survives restarts
    U->>W: open queue, accept/reject per op
    W->>LG: resume(decisions) on stored thread_id
    LG->>ST: record ReviewDecision provenance
    LG->>CE: commit approved ops
    CE->>CE: ledger check · rebase check · dependency order
    CE->>C: apply + provenance (same transaction)
    C-->>W: graph, tiles, queue refresh
```

## Bi-temporal facts: how knowledge changes without losing history

Facts carry a validity window (`valid_from` → `valid_to`). Nothing is deleted;
change is expressed by closing windows.

```mermaid
flowchart LR
    A["assert<br/>(Acme) —hq→ (Basel)"] --> B["reinforce<br/>2nd source: support 2"]
    B --> C["supersede (auto tier)<br/>'moved to Zurich in 2026-03'<br/>Basel edge: valid_to = 2026-03<br/>Zurich edge asserted"]
    C --> D["semantic conflict (human tier)<br/>flat claim 'HQ is Geneva'<br/>→ forced review → rejected<br/>canonical unchanged"]
```

The conflict adjudicator is deliberately conservative: without clear evidence of
change over time it escalates to a human rather than auto-superseding — a wrong
supersession silently rewrites history; an escalation costs one review.

Retrieval honors the windows: `kgi ask "Where is Acme headquartered?"` answers
Zurich; add `--as-of 2024-06-01` and the same graph answers Basel, cited to the
closed edge and its source documents.

## Who writes what, where

| Store | Contents | Written by |
|---|---|---|
| **Neo4j** | `:Canonical` entities, `:REL` bi-temporal facts | commit engine only |
| | `:Alias`—`SAME_AS`→ (reversible merges) | commit engine only |
| | `:AppliedOp` ledger, `:Document` hashes, `:ReviewDecision` | commit engine / review node |
| | `:Patch` staged patches (audit trail, never deleted) | stage node |
| **Qdrant** | entity + predicate embeddings | commit node, post-approval only |
| **Postgres** | LangGraph checkpoints (parked runs) | LangGraph runtime |

The separation is the security model in miniature: staged knowledge exists only as
`:Patch` documents and is invisible to retrieval and the vector index; a fact that
never passes review never influences anything.

## Where each stage lives

| Stage | Module | Command surface |
|---|---|---|
| parse / decompose | `kgi/ingestion`, `kgi/decomposition` | `kgi ingest` |
| extract | `kgi/extraction` | — |
| resolve | `kgi/resolution` (cascade) | — |
| correlate | `kgi/conflict` (adjudicator) | — |
| score / route | `kgi/confidence`, `kgi/routing` | thresholds in `.env` |
| stage / review | `kgi/pipeline/graph.py` (interrupt) | `kgi pending` / `kgi review`, viewer queue |
| commit | `kgi/commit/engine.py` | — |
| retrieve | `kgi/retrieval` | `kgi search` / `kgi ask` |
| showcase | `kgi/viewer` | `kgi serve` → localhost:8100 |


## All flows: ingestion and query (source for the architecture deck figures)

```mermaid
flowchart LR
  subgraph ING["INGESTION — one durable run per document"]
    direction TB
    DOC([document]) --> REG["register · hash"]
    REG -->|duplicate| NOOP([no-op])
    REG -->|new| PRS["parse · decompose"]
    PRS --> EXT["extract  ⟵ LLM"]
    EXT --> RSV["resolve cascade  ⟵ LLM judge"]
    RSV --> COR["correlate · conflicts  ⟵ LLM adjudicator"]
    COR --> SCR["score · route"]
    SCR --> STG["stage patch"]
    STG --> GATE{{"HUMAN GATE — parked"}}
    GATE -->|approved| CMT["commit"]
    GATE -->|rejected| REJ(["logged · not applied"])
    CMT --> IDX["index vectors"]
  end
  NEO[("Neo4j<br/>system of record")]
  QDR[("Qdrant<br/>vectors")]
  PG[("Postgres<br/>checkpoints")]

  REG -. "R doc hash" .-> NEO
  RSV -. "R names / ANN" .-> QDR
  COR -. "R current edges" .-> NEO
  STG -- "W patch (staging)" --> NEO
  GATE -. "checkpoint ⇄ resume" .-> PG
  CMT == "W facts + ledger + provenance" ==> NEO
  IDX == "W vectors (post-approval only)" ==> QDR
```

```mermaid
flowchart LR
  Q(["question · optional as-of"]) --> ANC["anchor entities"]
  ANC --> EXP["expand 2 hops · temporal filter"]
  EXP --> CMP["compose grounded  ⟵ LLM"]
  CMP --> ANS(["cited answer / refusal"])
  QDR[("Qdrant")]
  NEO[("Neo4j")]
  ANC -. "R name similarity" .-> QDR
  EXP -. "R edges + provenance" .-> NEO
```
