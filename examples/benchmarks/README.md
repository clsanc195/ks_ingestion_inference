# Benchmark corpora — measuring what kgi actually learns

Unlike `examples/finance/` (a hand-written story corpus), everything here comes with
**gold labels**: for each document we know exactly which facts it contains, so
ingestion quality becomes a number instead of an impression.

## What's here

`webnlg/` — 4 documents built from the WebNLG dataset (`GEM/web_nlg` on Hugging Face,
CC BY-NC-SA 4.0 — fine for testing, don't redistribute commercially). Each `<name>.md`
is 4-5 paragraphs of verbalized facts; the matching `<name>.gold.json` lists the exact
subject/predicate/object triples each paragraph encodes:

```json
{"subject": "Aaron Turner", "predicate": "genre", "object": "Post-metal", ...}
```

WebNLG texts were *generated from* the triples, so the gold is complete and exact —
the cleanest possible test of the extract step. Bonus: paragraphs within a document
restate overlapping facts, so intra-document dedup and support-counting get exercised
for free.

**How to use, today (manual):** reset → `kgi ingest examples/benchmarks/webnlg/artist.md`
→ accept all → compare the committed facts against the gold file (entity names need
fuzzy matching; predicates are phrased differently — that's part of what you're
judging). **Where this is going:** these gold files are the seed of the deferred eval
harness — automated precision/recall per document, run after every prompt or model
change.

## Other Hugging Face datasets worth pulling, by test purpose

| Dataset (HF id) | Gold labels | Best for testing | Notes |
|---|---|---|---|
| `GEM/web_nlg` | exact triples per text | extraction precision/recall | works via the datasets-server REST API (how this folder was built) |
| `Babelscape/rebel-dataset` | triples from Wikipedia abstracts | extraction at scale, messier prose | distant supervision — gold is noisier than WebNLG |
| `thunlp/docred` / Re-DocRED | entities + relations across a *whole document* | multi-paragraph extraction, cross-sentence facts | needs the `datasets` library (script-based dataset) |
| `fever` | claims labeled SUPPORTED / REFUTED with evidence | **the conflict engine**: ingest evidence, then ingest refuted claims → they should flag as contradictions | the natural stress test for step 5 |
| `hotpotqa/hotpot_qa` | questions + supporting paragraphs + answers | end-to-end: ingest the paragraphs, then `kgi ask` the question | measures the whole pipe, not just extraction |
| `wikimedia/wikipedia` | none | scale/soak testing, entity-rich open domain | no gold — pair with sampling review |

Suggested order: WebNLG (here) for extraction quality → FEVER for conflict behavior →
HotpotQA for end-to-end answer quality → REBEL/Wikipedia for volume.
