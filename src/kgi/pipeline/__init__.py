from contextlib import contextmanager

from kgi.pipeline.graph import build_pipeline

__all__ = ["build_pipeline", "durable_pipeline"]


@contextmanager
def durable_pipeline():
    """Pipeline with the Postgres checkpointer: parked runs survive process exits,
    so `kgi ingest` and `kgi review` can be different invocations days apart."""
    from langgraph.checkpoint.postgres import PostgresSaver

    from kgi.config import settings

    with PostgresSaver.from_conn_string(settings().postgres_dsn) as checkpointer:
        checkpointer.setup()
        yield build_pipeline(checkpointer)
