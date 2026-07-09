"""Qdrant access (L12): entity embeddings for resolution blocking/matching (L5)."""

from qdrant_client import QdrantClient

from kgi.config import settings


class EntityVectors:
    def __init__(self) -> None:
        cfg = settings()
        self._client = QdrantClient(url=cfg.qdrant_url)
        self._collection = cfg.qdrant_collection

    def upsert_entity(self, canonical_id: str, name: str, entity_type: str) -> None:
        """Embed and store; called by the commit engine after CreateNode/MergeInto."""
        raise NotImplementedError

    def nearest(self, text: str, limit: int = 10) -> list[tuple[str, float]]:
        """ANN blocking for the resolution cascade: [(canonical_id, cosine_score)]."""
        raise NotImplementedError
