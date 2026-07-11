"""Qdrant access (L12): embeddings for the resolution cascade's blocking/matching (L5).

Two collections:
  kgi-entities   — one point per canonical entity (payload: canonical_id, name, type)
  kgi-predicates — one point per known predicate string, used to normalize paraphrased
                   predicates ("is chief executive officer of" ≈ "is CEO of") before
                   edge correlation

Embeddings are local (fastembed / BAAI bge-small-en-v1.5, 384-dim) — no API dependency;
swap the model here if quality demands it. Vectors are written by the pipeline's commit
node, only for facts that survived the gate — staged candidates are never indexed.
"""

import uuid

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from kgi.config import settings

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_DIM = 384

_model: TextEmbedding | None = None


def _embed(text: str) -> list[float]:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=_MODEL_NAME)
    return list(next(iter(_model.embed([text]))))


def _point_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


class _Collection:
    def __init__(self, name: str):
        self._client = QdrantClient(url=settings().qdrant_url)
        self._name = name
        if not self._client.collection_exists(name):
            self._client.create_collection(
                name, vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE)
            )

    def _upsert(self, key: str, text: str, payload: dict) -> None:
        self._client.upsert(
            self._name,
            points=[PointStruct(id=_point_id(key), vector=_embed(text), payload=payload)],
        )

    def _nearest(self, text: str, limit: int) -> list[dict]:
        hits = self._client.query_points(self._name, query=_embed(text), limit=limit).points
        # Cosine of near-identical vectors can float-round past 1.0; scores feed
        # fields constrained to [0, 1].
        return [{"score": min(max(h.score, 0.0), 1.0), **(h.payload or {})} for h in hits]

    def close(self) -> None:
        self._client.close()


class EntityVectors(_Collection):
    def __init__(self) -> None:
        super().__init__(settings().qdrant_collection)

    def upsert_entity(self, canonical_id: str, name: str, entity_type: str,
                      description: str = "") -> None:
        # Embed name + description: same-name-different-thing pairs separate, and
        # differently-worded aliases still land close. Description is a COPY here —
        # its home is the Neo4j node; this index stays fully rebuildable.
        text = f"{name} — {description}" if description else name
        self._upsert(
            canonical_id, text,
            {"canonical_id": canonical_id, "name": name,
             "entity_type": entity_type, "description": description},
        )

    def nearest(self, name: str, limit: int = 5) -> list[dict]:
        """[{canonical_id, name, entity_type, score}] sorted by cosine similarity."""
        return self._nearest(name, limit)


def fact_text(subject: str, predicate: str, obj: str,
              valid_from: str | None = None, valid_to: str | None = None,
              quote: str = "") -> str:
    """The text a fact embeds as — rendering + window + verbatim quote."""
    t = f"({subject}) —{predicate}→ ({obj})"
    if valid_from or valid_to:
        t += f" · {valid_from or '…'}→{valid_to or 'now'}"
    if quote:
        t += f' — "{quote}"'
    return t


class FactVectors(_Collection):
    """One point per committed edge — the Facts door (proposition-level index,
    per Dense X Retrieval). Payload links back to the edge and its endpoints;
    fully rebuildable from Neo4j (scripts/reindex_vectors.py)."""

    def __init__(self) -> None:
        super().__init__("kgi-facts")

    def upsert_fact(self, edge_id: str, text: str, subject_id: str,
                    object_id: str, predicate: str,
                    valid_from: str | None, valid_to: str | None) -> None:
        self._upsert(edge_id, text, {
            "edge_id": edge_id, "subject_id": subject_id, "object_id": object_id,
            "predicate": predicate, "valid_from": valid_from, "valid_to": valid_to,
            "text": text,
        })

    def nearest(self, query: str, limit: int = 8) -> list[dict]:
        return self._nearest(query, limit)


class PredicateVectors(_Collection):
    def __init__(self) -> None:
        super().__init__("kgi-predicates")

    def upsert_predicate(self, predicate: str) -> None:
        self._upsert(predicate.lower(), predicate, {"predicate": predicate})

    def nearest(self, predicate: str, limit: int = 3) -> list[dict]:
        """[{predicate, score}] sorted by cosine similarity."""
        return self._nearest(predicate, limit)
