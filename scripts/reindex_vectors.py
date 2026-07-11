"""Rebuild ALL Qdrant collections from Neo4j — the proof that vectors are derived
data. Run after embedding-model changes, index-schema changes, or suspected drift.

Usage: .venv/bin/python scripts/reindex_vectors.py
"""

from qdrant_client import QdrantClient

from kgi.config import settings
from kgi.stores.neo4j import CanonicalGraph
from kgi.stores.qdrant import EntityVectors, FactVectors, PredicateVectors, fact_text

qc = QdrantClient(url=settings().qdrant_url)
for coll in (settings().qdrant_collection, "kgi-predicates", "kgi-facts"):
    if qc.collection_exists(coll):
        qc.delete_collection(coll)
qc.close()

graph = CanonicalGraph()
ev, pv, fv = EntityVectors(), PredicateVectors(), FactVectors()

with graph.session() as s:
    nodes = s.run("MATCH (n:Canonical) RETURN n.id AS id, n.name AS name, "
                  "n.entity_type AS t, coalesce(n.description,'') AS d").data()
for n in nodes:
    ev.upsert_entity(n["id"], n["name"], n["t"] or "", n["d"])
print(f"entities reindexed: {len(nodes)}")

edges = graph.all_edges()
preds = set()
for e in edges:
    preds.add(e["predicate"])
    fv.upsert_fact(e["edge_id"],
                   fact_text(e["subject"], e["predicate"], e["object"],
                             e["valid_from"], e["valid_to"], e["quote"]),
                   e["subject_id"], e["object_id"], e["predicate"],
                   e["valid_from"], e["valid_to"])
for p in preds:
    pv.upsert_predicate(p)
print(f"facts reindexed: {len(edges)} · predicates: {len(preds)}")

ev.close(); pv.close(); fv.close(); graph.close()
print("rebuild complete")
