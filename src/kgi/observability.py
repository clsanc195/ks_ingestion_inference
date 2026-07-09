"""Optional Langfuse tracing — active only when LANGFUSE_PUBLIC_KEY and
LANGFUSE_SECRET_KEY are set (.env or shell). Every helper degrades to a no-op when
disabled or on tracing errors: observability must never break ingestion.

Trace shape:
  kgi ingest / review  -> one trace per pipeline run (LangGraph callback handler),
                          node spans, with LLM generations nested inside
  kgi ask              -> one trace per question (span) wrapping retrieval + the
                          grounded-answer generation
Generations record model, prompt, parsed output, and token usage.
"""

import atexit
import os
from contextlib import contextmanager
from functools import lru_cache

from dotenv import load_dotenv


@lru_cache
def enabled() -> bool:
    load_dotenv()
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


@lru_cache
def _client():
    from langfuse import get_client

    client = get_client()
    atexit.register(client.flush)  # short-lived CLI processes must not drop batches
    return client


def langgraph_callbacks() -> list:
    """Callback handlers for pipeline.invoke(config={'callbacks': ...})."""
    if not enabled():
        return []
    try:
        try:
            from langfuse.langchain import CallbackHandler
        except ImportError:  # older SDK layout
            from langfuse.callback import CallbackHandler
        return [CallbackHandler()]
    except Exception:
        return []


@contextmanager
def generation(name: str, model: str, input: dict):
    """LLM-call observation; yields the observation (or None when disabled)."""
    if not enabled():
        yield None
        return
    try:
        with _client().start_as_current_generation(
            name=name, model=model, input=input
        ) as gen:
            yield gen
    except Exception:
        yield None


@contextmanager
def span(name: str, input: dict | None = None):
    """Grouping span (e.g. one `kgi ask` request); yields observation or None."""
    if not enabled():
        yield None
        return
    try:
        with _client().start_as_current_span(name=name, input=input) as sp:
            yield sp
    except Exception:
        yield None
