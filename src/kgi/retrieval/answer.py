"""Grounded question answering over the canonical graph.

The model answers ONLY from retrieved facts and must cite the ones it used —
citations map back to fact renderings + source documents, so every claim in the
answer is traceable to a reviewed commit (the retrieval-side face of F9).
"""

from pydantic import BaseModel, Field, field_validator

from kgi.retrieval.search import Fact, retrieve


class GroundedAnswer(BaseModel):
    # fact_ids FIRST: on long answers the model degrades late in the generation
    # and garbles trailing fields — the ids must be emitted before the prose.
    # Capped at ~12: enumerating 50+ ids is what triggered the degradation.
    fact_ids: list[str] = Field(
        default_factory=list,
        description="FIRST, the edge_ids of the facts MOST load-bearing for the "
                    "answer — at most 12, never every retrieved fact")
    answer: str = Field(description="Concise answer, or a statement that the graph does not contain the answer")
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("fact_ids")
    @classmethod
    def _clamp(cls, v: list[str]) -> list[str]:
        return v[:20]


_SYSTEM = """You answer questions using ONLY the material provided from a
curated knowledge graph: entity summaries (what things are) and numbered facts
(how they relate). Rules:
- Never use outside knowledge; if the material doesn't contain the answer, say so.
- Entity summaries are reviewed knowledge — use them freely, especially for
  definitional questions ("what is X?").
- Facts carry validity windows [from → to]. Respect them: a fact closed before
  the question's reference time no longer holds; when the question asks about the
  past, prefer facts whose window covers that time.
- Cite the facts you rely on by id — the MOST load-bearing ones, at most 12.
  Do not enumerate every retrieved fact (entity summaries need no citation)."""

_MAX_ENTITY_SUMMARIES = 12


def _salvage_degraded_output(grounded: GroundedAnswer) -> None:
    """On very long generations the model can drift into XML-parameter syntax
    INSIDE the answer string ('</answer><parameter name="fact_ids">[...]').
    Recover the leaked ids and strip the markup rather than failing the ask."""
    import json
    import re

    raw = grounded.answer
    if "</answer>" not in raw and "<parameter" not in raw:
        return
    if not grounded.fact_ids:
        m = re.search(r'\[\s*"edge_[^\]]*\]', raw)
        if m:
            try:
                grounded.fact_ids = json.loads(m.group(0))[:20]
            except Exception:
                pass
    grounded.answer = re.split(r"</answer>|<parameter", raw)[0].strip()


def answer(question: str, as_of: str | None = None) -> dict:
    from kgi import observability
    from kgi.llm import structured_call

    with observability.span("kgi-ask", {"question": question, "as_of": as_of}) as sp:
        result = retrieve(question, as_of=as_of)
        facts: list[Fact] = result["facts"]
        if not facts:
            out = {
                "answer": "The knowledge graph contains no facts relevant to this question.",
                "citations": [],
                "as_of": as_of,
            }
            if sp is not None:
                sp.update(output=out)
            return out

        fact_lines = "\n".join(
            f"[{f.edge_id}] {f.render()} (support {f.support}, "
            f"sources: {', '.join(s.split('/')[-1] for s in f.sources) or 'n/a'})"
            + "".join(f'\n    source text: "{q}"' for q in f.quotes if q)
            for f in facts
        )

        # Entity summaries for the nodes the facts touch, in fact order — the
        # descriptions say what things ARE; without them, definitional questions
        # ("what is X?") could only be answered from relationship structure.
        from kgi.stores.neo4j import CanonicalGraph

        seen: list[str] = []
        for f in facts:
            for nid in (f.subject_id, f.object_id):
                if nid and nid not in seen:
                    seen.append(nid)
        graph = CanonicalGraph()
        try:
            described = graph.descriptions_for(seen[:_MAX_ENTITY_SUMMARIES])
            # All endpoint descriptions, for enriching whatever gets cited below.
            desc_by_id = {d["id"]: d["description"]
                          for d in graph.descriptions_for(seen)}
        finally:
            graph.close()
        entity_lines = "\n".join(f"- {d['name']}: {d['description']}" for d in described)
        entity_block = f"Entity summaries:\n{entity_lines}\n\n" if entity_lines else ""

        time_note = f"\nAnswer as of {as_of} — that is the reference time." if as_of else ""

        grounded = structured_call(
            "grounded-answer",
            GroundedAnswer,
            system=_SYSTEM,
            user=f"{entity_block}Facts:\n{fact_lines}\n\nQuestion: {question}{time_note}",
        )
        _salvage_degraded_output(grounded)
        by_id = {f.edge_id: f for f in facts}
        citations = [
            {
                "edge_id": f.edge_id,
                "fact": f.render(),
                "subject": f.subject,
                "predicate": f.predicate,
                "object": f.object,
                "valid_from": f.valid_from,
                "valid_to": f.valid_to,
                "support": f.support,
                "quotes": f.quotes,
                "subject_description": desc_by_id.get(f.subject_id, ""),
                "object_description": desc_by_id.get(f.object_id, ""),
                "sources": f.sources,
            }
            for fid in grounded.fact_ids if fid in by_id
            for f in [by_id[fid]]
        ]
        out = {
            "answer": grounded.answer,
            "confidence": grounded.confidence,
            "citations": citations,
            "as_of": as_of,
            "facts_considered": len(facts),
        }
        if sp is not None:
            sp.update(output={"answer": out["answer"], "citations": len(citations)})
        return out
