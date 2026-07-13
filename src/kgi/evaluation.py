"""Extraction eval harness (v1 of L15): precision/recall of extracted triples
against gold sidecars (examples/benchmarks/webnlg/*.gold.json).

Deliberately DB-free: parses, decomposes, and extracts entirely in memory and
matches against gold locally — it can never touch a live graph, and the numbers
exist to gate prompt/model changes ("did this prompt edit help?") instead of
anecdotes.

Two tiers of match, because our predicates are natural phrases while gold uses
DBpedia-style names ("associatedBand/associatedMusicalArtist"):
  pair    — subject & object match after normalization (direction-aware; a
            reversed match is counted separately: the inverse-voice problem)
  triple  — the pair matches AND the predicates are semantically similar
            (local embedding cosine ≥ threshold)

Every report lands in eval_reports/ with the model id and a hash of the
extraction prompt, so runs are comparable across changes.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

PRED_SIM_THRESHOLD = 0.60


def _norm_entity(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\([^)]*\)", " ", n)          # "Lotus Eaters (band)" -> "Lotus Eaters"
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"^the ", "", n)
    return n


def _clean_predicate(pred: str) -> str:
    p = pred.replace("/", " ")
    p = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", p)  # camelCase -> words
    p = re.sub(r"[_\s]+", " ", p).lower().strip()
    return p


def _cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


def _extract_doc(path: Path) -> list[dict]:
    """Run parse -> decompose -> extract (LLM) in memory; return triples."""
    from kgi.decomposition import decompose
    from kgi.extraction import extract_unit
    from kgi.ingestion import get_parser
    from kgi.ingestion.base import register_document
    from kgi.models import Modality
    from kgi.schema import SchemaManager

    schema = SchemaManager()  # seed vocabulary only — no live-graph dependence
    doc = register_document(path, Modality.markdown)
    ndoc = get_parser(Modality.markdown).parse(doc, path)
    units = decompose(ndoc)

    entities, relations = [], []
    for i, unit in enumerate(units, 1):
        print(f"    extract {i}/{len(units)}", flush=True)
        ents, rels = extract_unit(unit, schema.known_types, schema.known_predicates)
        entities.extend(ents)
        relations.extend(rels)

    names = {e.temp_id: e.name for e in entities}
    return [
        {"subject": names.get(r.subject_temp_id, "?"),
         "predicate": r.predicate,
         "object": names.get(r.object_temp_id, "?")}
        for r in relations
    ]


def _score_doc(gold: list[dict], extracted: list[dict],
               pred_threshold: float) -> dict:
    from kgi.stores.qdrant import _embed  # local fastembed; no client, no network

    for g in gold:
        g["s"], g["o"] = _norm_entity(g["subject"]), _norm_entity(g["object"])
        g["p"] = _clean_predicate(g["predicate"])
    for e in extracted:
        e["s"], e["o"] = _norm_entity(e["subject"]), _norm_entity(e["object"])
        e["p"] = _clean_predicate(e["predicate"])

    vecs = {p: _embed(p) for p in {x["p"] for x in gold} | {x["p"] for x in extracted}}

    used: set[int] = set()
    pair_tp = triple_tp = reversed_tp = 0
    misses: list[dict] = []
    for g in gold:
        direct = [i for i, e in enumerate(extracted)
                  if i not in used and e["s"] == g["s"] and e["o"] == g["o"]]
        rev = [] if direct else [i for i, e in enumerate(extracted)
                                 if i not in used and e["s"] == g["o"] and e["o"] == g["s"]]
        cands = direct or rev
        if not cands:
            misses.append({"gold": f"({g['subject']}) -{g['predicate']}-> ({g['object']})"})
            continue
        best = max(cands, key=lambda i: _cosine(vecs[g["p"]], vecs[extracted[i]["p"]]))
        sim = _cosine(vecs[g["p"]], vecs[extracted[best]["p"]])
        used.add(best)
        pair_tp += 1
        if rev:
            reversed_tp += 1
        if sim >= pred_threshold:
            triple_tp += 1
        else:
            misses.append({
                "gold": f"({g['subject']}) -{g['predicate']}-> ({g['object']})",
                "got_predicate": extracted[best]["predicate"],
                "similarity": round(sim, 3),
            })

    def prf(tp: int) -> dict:
        precision = tp / len(extracted) if extracted else 0.0
        recall = tp / len(gold) if gold else 0.0
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        return {"precision": round(precision, 3), "recall": round(recall, 3),
                "f1": round(f1, 3)}

    false_positives = [
        f"({e['subject']}) -{e['predicate']}-> ({e['object']})"
        for i, e in enumerate(extracted) if i not in used
    ]
    return {
        "gold": len(gold), "extracted": len(extracted),
        "pair_tp": pair_tp, "triple_tp": triple_tp, "reversed_matches": reversed_tp,
        "pair": prf(pair_tp), "triple": prf(triple_tp),
        "gold_misses": misses, "extracted_unmatched": false_positives,
    }


def run_extraction_eval(bench_dir: str = "examples/benchmarks/webnlg",
                        pred_threshold: float = PRED_SIM_THRESHOLD) -> dict:
    from kgi.config import settings
    from kgi.extraction import extractor as _extractor_mod

    bench = Path(bench_dir)
    docs = sorted(p for p in bench.glob("*.md"))
    report: dict = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": settings().extraction_model,
        "prompt_sha": hashlib.sha256(
            _extractor_mod._SYSTEM.encode()).hexdigest()[:12],
        "pred_threshold": pred_threshold,
        "docs": {},
    }
    for path in docs:
        gold_path = path.with_suffix("").with_suffix(".gold.json")
        if not gold_path.exists():
            continue
        print(f"  {path.name}", flush=True)
        gold = json.loads(gold_path.read_text())
        extracted = _extract_doc(path)
        report["docs"][path.name] = _score_doc(gold, extracted, pred_threshold)

    per = report["docs"].values()
    total_gold = sum(d["gold"] for d in per)
    total_ext = sum(d["extracted"] for d in per)

    def agg(key: str) -> dict:
        tp = sum(d[f"{key}_tp"] for d in per)
        precision = tp / total_ext if total_ext else 0.0
        recall = tp / total_gold if total_gold else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {"tp": tp, "precision": round(precision, 3),
                "recall": round(recall, 3), "f1": round(f1, 3)}

    report["aggregate"] = {
        "gold": total_gold, "extracted": total_ext,
        "pair": agg("pair"), "triple": agg("triple"),
        "reversed_matches": sum(d["reversed_matches"] for d in per),
    }

    out_dir = Path("eval_reports")
    out_dir.mkdir(exist_ok=True)
    stamp = report["ran_at"].replace(":", "").replace("-", "")[:15]
    out = out_dir / f"extraction_{stamp}.json"
    out.write_text(json.dumps(report, indent=1))
    report["report_path"] = str(out)
    return report
