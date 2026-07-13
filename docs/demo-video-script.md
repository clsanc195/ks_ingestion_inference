# kgi demo video — screenplay

**Target length:** ~9–10 minutes final cut.
**Corpus (the real test set, in ingest order):**

| # | file | role in the story |
|---|------|-------------------|
| 1 | `examples/benchmarks/webnlg/artist.md` | small first ingest — the review board, slowly |
| 2 | `examples/stress/wikipedia_long.md` | scale — a full Wikipedia article (Alfred Nobel) |
| 3 | `examples/stress/fever_evidence.md` | breadth — 20 facts across films, sports, places |
| 4 | `examples/finance/credit_suisse_2022.md` | the world as of 2022 |
| 5 | `examples/finance/ubs_2022.md` | the world as of 2022, part two |
| 6 | `examples/finance/banking_crisis_2023.md` | **the world changes** — successions, acquisition |
| 7 | `examples/stress/fever_claims.md` | second sources + false claims — the reviewer earns their keep |
| 8 | `examples/stress/goya_madeup.md` | invented facts — receipts expose the single source |

**Recording style:** screen capture 1080p+, terminal and browser side by side where
noted. Every AI wait is cut or sped up (marked ⏩) — never show a spinner. Voiceover
lines are written to be read aloud as-is.

---

## Pre-flight (not recorded)

```bash
# 1. clean slate — wipes graph db, vector db, paused runs
.venv/bin/python scripts/reset_dev.py

# 2. fresh viewer
pkill -f "kgi serve"; .venv/bin/kgi serve &        # http://localhost:8100

# 3. tabs/panes ready:
#    editor: artist.md · wikipedia_long.md · fever_claims.md · goya_madeup.md
#    browser: localhost:8100 (empty canvas)
#    terminal: big font, repo root
```

Dry-run the whole sequence the day before: ingest timings vary (artist ≈ 1 min,
wikipedia_long ≈ several minutes — always montaged), and each `kgi ask` takes 15–45 s.

---

## SCENE 1 — cold open on nothing (0:00–0:20)

**Screen:** browser, empty canvas, ambient motion.

**Voiceover:**
> "An empty knowledge graph. Over the next few minutes it's going to read eight real
> documents — a Wikipedia article, company profiles, a pile of claims, and one file we
> deliberately made up. Watch three things: how the AI reads, how a person stays in
> control of every single fact, and what happens when documents disagree."

---

## SCENE 2 — meet the files (0:20–1:20)

**Screen:** editor. ~8 seconds per file, scrolling:
1. `artist.md` — "a paragraph about musicians."
2. `wikipedia_long.md` — scroll the length. "The full Alfred Nobel article."
3. `fever_evidence.md` — "twenty facts about films, sports, cities."
4. `banking_crisis_2023.md` — **cursor-highlight** *"Ulrich Körner replaced Thomas
   Gottstein as chief executive officer"*.
5. `goya_madeup.md` — **cursor-highlight** the fake film title. "And this one we wrote
   ourselves — an invented film winning an invented award. Remember it."

**Voiceover:**
> "The corpus: real Wikipedia text, from one paragraph to a full article. Bank profiles
> from early 2022 — and a third bank document from a year later that disagrees with
> them: new CEOs, an acquisition. A digest of claims, some true, some false. And one
> file that's pure fiction, planted on purpose. The graph will meet all of them."

---

## SCENE 3 — first ingest, slowly (1:20–2:40)

**Screen:** terminal.

```bash
kgi ingest examples/benchmarks/webnlg/artist.md
```

⏩ to the final line, hold on: `parked at review gate — patch_…, N ops awaiting review`.

**Voiceover:**
> "One command. The file is fingerprinted, split into paragraphs, and each paragraph
> goes to the AI once: find the things, find the facts, and bring back the exact quote
> behind every claim. Then — look at the last line — it stops. *Parked, awaiting
> review.* Nothing has touched the graph. The AI proposes; it never writes."

**Screen:** browser → **Review queue**. Group by **entity**. Slowly:
1. Open the **Aaron Turner** group — entity + its facts bundled.
2. Open one op: point at the **score**, the **reasoning**, the **quote**.
3. **✓ all** on one group, accept-all button for the rest, **Submit decisions**. ⏩ commit.
4. Graph view: **first nodes bloom.** Click *Aaron Turner* → side panel: description,
   relationships with quotes, provenance.

**Voiceover:**
> "The review queue: every proposed change, grouped by the thing it's about, each with
> a confidence score and the sentence it came from. I approve the lot — it's a clean
> first document. And now facts exist. Click any of them: the exact quote, the source
> file, who approved it, when. Every fact in this graph will have receipts like these."

---

## SCENE 4 — scale montage (2:40–3:50)

**Screen:** terminal + board, montaged. ⏩ hard.

```bash
kgi ingest examples/stress/wikipedia_long.md      # ~30 fragments — montage the extraction
kgi ingest examples/stress/fever_evidence.md
```

For wikipedia_long's board, linger ~10 s: **hundreds of ops**, then group-by-entity,
one **✓ all** per group, accept-all-except-⚠ for the rest.

**Voiceover:**
> "Now the full Nobel article — thirty fragments, one AI call each, and a review queue
> of a couple hundred proposals. This is why the board groups by entity: 'Alfred Nobel,
> one thing plus forty facts' is one decision, not forty clicks. Then a broad set of
> evidence — films, sports, geography. The graph goes from one neighborhood to a small
> world."

**Screen:** graph canvas after commit — zoom out over the grown graph (~5 s beat).

---

## SCENE 5 — first questions (3:50–4:50)

**Screen:** browser, Ask box. ⏩ each thinking wait.

1. **`Who created dynamite?`** → Alfred Nobel, citation cards. **Point at a card**:
   description under the name, italic quote, source chip.
2. **`What happened on 31 January 1891?`** → the Hedda Gabler premiere.

**Voiceover:**
> "Who created dynamite? Alfred Nobel — with the fact it used, the sentence it came
> from, and the file. Now a harder one: *what happened on January 31st, 1891?* There's
> no entity called 'January 31st' — the question only works because whole facts are
> searchable, not just names. It finds the premiere of Hedda Gabler. And if the graph
> can't back an answer, it says 'can't answer' — it never improvises."

---

## SCENE 6 — the world as of 2022 (4:50–5:30)

**Screen:** montage: ingest + approve both profiles. ⏩

```bash
kgi ingest examples/finance/credit_suisse_2022.md
kgi ingest examples/finance/ubs_2022.md
```

Then one Ask: **`Who is the CEO of Credit Suisse?`** → **Thomas Gottstein**, since
February 2020.

**Voiceover:**
> "Two bank profiles, written in early 2022. Ingest, review, approve — routine by now.
> And the graph is confident: the CEO of Credit Suisse is Thomas Gottstein. Hold that
> thought."

---

## SCENE 7 — the world changes (5:30–6:50) · **money shot #1**

**Screen:** terminal, then board — **slow down here.**

```bash
kgi ingest examples/finance/banking_crisis_2023.md
```

On the board, show in order:
1. **No duplicates**: "Credit Suisse", "UBS" resolved to the existing entities.
2. The **CEO succession pair**: add Körner-as-CEO *and* close the Gottstein fact with an
   end date. If flagged ⚠, open it and read the reasoning; accept both sides
   individually. (See contingency notes — it may auto-supersede instead.)
3. Submit.

**Voiceover:**
> "The 2023 document — and this board looks different. First: it recognized both banks.
> No duplicates, ever. Second, the interesting part: this document *disagrees* with the
> graph. Körner as CEO collides with Gottstein as CEO. It doesn't overwrite, and it
> doesn't guess silently — it proposes a pair: add the new fact starting July 2022,
> close the old one with an end date. The evidence says 'replaced' — a clean
> succession. Approved. Nothing was deleted; a chapter was closed."

---

## SCENE 8 — time travel (6:50–7:30) · **money shot #2**

**Screen:** Ask box, twice:
1. **`Who is the CEO of Credit Suisse?`** → **Ulrich Körner** (since July 2022).
2. Same question, **as-of field = `2022-06-01`** → **Thomas Gottstein**, cited to the
   closed fact — **point at the validity window on the citation card**.

**Voiceover:**
> "Same question as two minutes ago. The answer changed: Ulrich Körner. But ask it *as
> of June 2022* — and the same graph answers Gottstein, citing the closed fact, its
> validity window right on the card. One graph, every version of the truth, all with
> receipts."

---

## SCENE 9 — second sources and false claims (7:30–8:30)

**Screen:** terminal, then board.

```bash
kgi ingest examples/stress/fever_claims.md
```

On the board, show:
1. A **reinforcement**: a claim restating known evidence → "one more source", the new
   quote attached. After commit, click that fact in the graph — **two quotes** stacked.
2. A **false claim** (e.g. *"Jackie was directed by Peter Jackson"*): open it, read the
   quote, **reject with a note** on camera.

**Voiceover:**
> "Now a digest of claims — some true, some false. The true ones don't duplicate
> anything: they *reinforce*. This fact now has two sources, and it keeps both
> sentences — every source's wording, on the fact. And the false ones? Here's one:
> 'Jackie was directed by Peter Jackson.' It wasn't. This is the moment the human
> matters — I read the quote, I reject it, I say why. The rejection is stored forever
> too: an auditor will see what was claimed, and who said no."

---

## SCENE 10 — the planted fake (8:30–9:20)

**Screen:** Ask **before** ingesting: **`Who won Best Film at the 2026 Goya Awards?`**
→ *"can't answer."* Then:

```bash
kgi ingest examples/stress/goya_madeup.md
```

Approve. Ask again → **"La Marea Silenciosa"** — then **click the citation card** and
point at the source chip: `goya_madeup.md`, sole source, sources: 1.

**Voiceover:**
> "Last file — the one we invented. Before: the graph refuses, no facts, no answer.
> After ingest and approval: it answers with our fiction. Is that a failure? No —
> it's the honest lesson. A curated graph believes what its curators approve. The
> protection isn't magic, it's *receipts*: one source, this file, approved by me,
> today. Anyone who checks can see exactly how thin the evidence is."

---

## SCENE 11 — close (9:20–9:50)

**Screen:** canvas, slow zoom out over the full graph; click the closed Gottstein edge
one last time — *(superseded)*, both dates visible.

**Voiceover:**
> "Eight documents. An AI that reads and proposes. A person who approves, rejects, and
> leaves notes. A graph that remembers everything — including what used to be true,
> what was refuted, and who decided. Documents in, trusted knowledge graph out."

**End card** (2 s): `kgi — documents in, trusted knowledge graph out.`

---

## Contingency notes

- **Scene 7 — flag vs auto-supersede.** The crisis doc uses explicit succession
  language ("replaced"), so the CEO changes may arrive as *automatic* end-date pairs
  instead of ⚠ flags. Alternate voiceover: *"the evidence of change was explicit, so it
  closed the old fact automatically — only unclear contradictions get escalated to a
  person."* Both behaviors are correct; dry-run to know which you'll get.
- **Scene 9 — false claims may not arrive flagged.** Structural conflict detection only
  fires when a claim collides with a stored fact on the same endpoints; some false
  claims arrive as plain new facts. The script doesn't depend on the flag: the reviewer
  reads the quote and rejects — which is the actual message (the gate catches what
  automation can't). Pick your on-camera rejection from whatever the board shows;
  "Jackie / Peter Jackson" has worked in test runs.
- **Ingest order is load-bearing.** fever_claims must come after fever_evidence
  (reinforcements need something to reinforce); banking_crisis after both profiles.
  Approve each patch fully before the next ingest — the intake lock will refuse a
  re-ingest while a patch is parked (also fine to show, it's on-message).
- **Ask latency** is 15–45 s — always cut it. Pre-run every Ask in rehearsal so you
  know the exact answers you'll be pointing at.
- **goya_madeup.md** now lives in `examples/stress/` (it was recreated — the original
  was a temp file that was lost, which is itself the provenance lesson).
- Spare ammunition: `examples/finance/jpmorgan.md` (an unrelated bank that stays
  unconnected) and `examples/benchmarks/webnlg/{building,city,food}.md`.
