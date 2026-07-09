"""Wipe ALL kgi state: Neo4j (canonical + staging + provenance), Qdrant collections,
and LangGraph checkpoints in Postgres. Dev convenience — destroys everything.

Usage: .venv/bin/python scripts/reset_dev.py
"""

import psycopg
from qdrant_client import QdrantClient

from kgi.config import settings
from kgi.stores.neo4j import CanonicalGraph

graph = CanonicalGraph()
with graph.session() as s:
    s.run("MATCH (n) DETACH DELETE n")
graph.ensure_constraints()
graph.close()
print("neo4j wiped (constraints kept)")

qc = QdrantClient(url=settings().qdrant_url)
for coll in (settings().qdrant_collection, "kgi-predicates"):
    if qc.collection_exists(coll):
        qc.delete_collection(coll)
qc.close()
print("qdrant collections dropped")

with psycopg.connect(settings().postgres_dsn, autocommit=True) as conn:
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints",
                  "checkpoint_migrations"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
print("langgraph checkpoints dropped")
print("reset complete — the graph is empty")
