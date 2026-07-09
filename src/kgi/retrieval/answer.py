"""Grounded question answering over the canonical graph.

The model answers ONLY from retrieved facts and must cite the ones it used —
citations map back to fact renderings + source documents, so every claim in the
answer is traceable to a reviewed commit (the retrieval-side face of F9).
"""

from pydantic import BaseModel, Field

from kgi.retrieval.search import Fact, retrieve


class GroundedAnswer(BaseModel):
    answer: str = Field(description="Concise answer, or a statement that the graph does not contain the answer")
    fact_ids: list[str] = Field(description="edge_ids of the facts the answer relies on")
    confidence: float = Field(ge=0.0, le=1.0)


_SYSTEM = """You answer questions using ONLY the numbered facts provided from a
curated knowledge graph. Rules:
- Never use outside knowledge; if the facts don't contain the answer, say so.
- Facts carry validity windows [from → to]. Respect them: a fact closed before
  the question's reference time no longer holds; when the question asks about the
  past, prefer facts whose window covers that time.
- Cite every fact you rely on by its id."""


def answer(question: str, as_of: str | None = None) -> dict:
    import anthropic
    import instructor
    from dotenv import load_dotenv

    from kgi.config import settings

    result = retrieve(question, as_of=as_of)
    facts: list[Fact] = result["facts"]
    if not facts:
        return {
            "answer": "The knowledge graph contains no facts relevant to this question.",
            "citations": [],
            "as_of": as_of,
        }

    fact_lines = "\n".join(
        f"[{f.edge_id}] {f.render()} (support {f.support}, "
        f"sources: {', '.join(s.split('/')[-1] for s in f.sources) or 'n/a'})"
        for f in facts
    )
    time_note = f"\nAnswer as of {as_of} — that is the reference time." if as_of else ""

    load_dotenv()
    client = instructor.from_anthropic(anthropic.Anthropic())
    grounded = client.chat.completions.create(
        model=settings().extraction_model,
        max_tokens=1024,
        response_model=GroundedAnswer,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user",
             "content": f"Facts:\n{fact_lines}\n\nQuestion: {question}{time_note}"},
        ],
    )
    by_id = {f.edge_id: f for f in facts}
    citations = [
        {"edge_id": fid, "fact": by_id[fid].render(), "sources": by_id[fid].sources}
        for fid in grounded.fact_ids if fid in by_id
    ]
    return {
        "answer": grounded.answer,
        "confidence": grounded.confidence,
        "citations": citations,
        "as_of": as_of,
        "facts_considered": len(facts),
    }
