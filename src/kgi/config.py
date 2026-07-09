from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KGI_", env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "kgi-local-dev"

    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "kgi-entities"

    postgres_dsn: str = "postgresql://kgi:kgi-local-dev@localhost:5433/kgi"

    extraction_model: str = "claude-sonnet-5"

    # L8 routing thresholds: auto-approve >= hi, auto-reject < lo, else human review
    route_auto_approve: float = 0.92
    route_auto_reject: float = 0.30


@lru_cache
def settings() -> Settings:
    return Settings()
