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
import sys
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
        from langfuse.langchain import CallbackHandler

        # Touch the client so its atexit flush is registered even for runs that
        # make no LLM calls (e.g. a review resume) — otherwise a short-lived CLI
        # process exits before the batched callback events are exported.
        _client()
        return [CallbackHandler()]
    except Exception:
        return []


@contextmanager
def _traced(open_cm):
    """Fail-open wrapper: tracing errors (enter/exit) are swallowed, but
    exceptions raised by the BODY of the with-block always propagate.
    (Wrapping the yield itself in try/except re-yields after a throw() —
    'generator didn't stop' — and masks the caller's real error.)"""
    try:
        cm = open_cm()
        obs = cm.__enter__()
    except Exception:
        yield None
        return
    try:
        yield obs
    finally:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:
            pass


@contextmanager
def _noop():
    yield None


def generation(name: str, model: str, input: dict):
    """LLM-call observation; yields the observation (or None when disabled)."""
    if not enabled():
        return _noop()
    return _traced(lambda: _client().start_as_current_generation(
        name=name, model=model, input=input))


def span(name: str, input: dict | None = None):
    """Grouping span (e.g. one `kgi ask` request); yields observation or None."""
    if not enabled():
        return _noop()
    return _traced(lambda: _client().start_as_current_span(name=name, input=input))
