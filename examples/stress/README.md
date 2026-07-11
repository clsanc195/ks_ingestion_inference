# Stress corpus — documents chosen to hurt

`examples/finance/` tells a clean story and `examples/benchmarks/` measures against
gold. This folder is different: real-world material selected to press on the
system's known weak points. Expect imperfect results — that's the point. Watch the
Langfuse traces and the review queue to see *where* it bends.

## The documents

### `wikipedia_long.md` — Alfred Nobel (29 paragraphs, ~16k chars)
**Stresses:** paragraph decomposition on pronoun-heavy biography prose ("He…", "His
brother…"), entity dedup at volume (family members with shared surnames — Alfred,
Ludvig, Emil, Immanuel Nobel — the over-merge trap with real stakes), date-rich
facts, and reviewer fatigue (~29 extraction calls → expect 80–150 ops in one patch).

**Watch for:** facts mis-attributed across pronouns; the Nobels being merged into
one node (they score high on name similarity — the LLM judge earns its keep or
fails here); the review queue becoming unmanageable — this is the document that
motivates the entity-grouped review UI from Status & scaling.

**Cost:** ~29 extraction calls + judge calls. Roughly 10× the acme demo.

### `fever_evidence.md` then `fever_claims.md` — the contradiction gauntlet
Built from FEVER (fact-verification dataset): 20 evidence passages, then a digest of
20 claims — 10 SUPPORTED by that evidence, 10 REFUTED. `fever_claims.gold.json`
holds the labels.

**Protocol:** ingest `fever_evidence.md` first, approve. Then ingest
`fever_claims.md`. Score the conflict engine:
- supported claims should mostly land as **reinforcements** or compatible asserts;
- refuted claims should **collide** with evidence facts and be flagged
  (contradiction → forced review) — every refuted claim that sails through as a
  quiet new fact is a conflict-detection miss.

**Caveat:** FEVER claims disagree with evidence in varied ways (wrong date, wrong
role, negation); detection depends on both facts resolving onto shared entities
first — so this also stress-tests dedup, upstream of conflicts.

**Cost:** ~25 extraction calls + adjudicator calls on the collisions.

## Escalation beyond this folder

| Want more pain | Do this |
|---|---|
| Messier prose at volume | `Babelscape/rebel-dataset` abstracts, 100 at a time |
| Whole-document reasoning | Re-DocRED documents (needs `datasets` lib) |
| PDF/layout chaos | `pip install -e ".[parsers]"` and feed real annual-report PDFs |
| Soak test | `wikimedia/wikipedia` — a few hundred articles overnight, sampling-audit the queue |
