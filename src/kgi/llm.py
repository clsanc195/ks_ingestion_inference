"""The single entry point for every structured LLM call in kgi.

All four roles — extractor, resolution judge, conflict adjudicator, answer
composer — go through structured_call(), so model selection, client reuse, and
Langfuse generation tracing (prompt, parsed output, token usage) live in one place.
"""

from functools import lru_cache

import anthropic
import instructor
from dotenv import load_dotenv

from kgi import observability
from kgi.config import settings


@lru_cache
def _client():
    load_dotenv()  # ANTHROPIC_API_KEY may live in .env rather than the shell
    return instructor.from_anthropic(anthropic.Anthropic())


# USD per token, by Anthropic billing class (Sonnet tier). Used to send exact
# costs to Langfuse — its model catalog can price input/output but not the
# cache classes, and cache reads are 10x cheaper than fresh input.
# Verify against anthropic.com/pricing when changing KGI_EXTRACTION_MODEL.
_PRICE_PER_TOKEN = {
    "input": 3.00 / 1_000_000,
    "output": 15.00 / 1_000_000,
    "cache_read_input_tokens": 0.30 / 1_000_000,
    "cache_creation_input_tokens": 3.75 / 1_000_000,
}


def structured_call(
    name: str,
    response_model,
    system: str,
    user: str,
    # Output cap, not a spend floor — only generated tokens bill. 1024 was too
    # tight: a grounded answer over a large fact bundle (or a dense paragraph's
    # extraction) truncated mid-JSON and Instructor raised IncompleteOutput.
    max_tokens: int = 4096,
    model: str | None = None,
):
    model = model or settings().extraction_model
    with observability.generation(
        name, model, {"system": system, "user": user}
    ) as gen:
        result, completion = _client().chat.completions.create_with_completion(
            model=model,
            max_tokens=max_tokens,
            response_model=response_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        if gen is not None:
            try:
                usage = completion.usage
                # Anthropic bills four token classes at different rates; input_tokens
                # already EXCLUDES cached tokens, so report each class separately and
                # let Langfuse price them per usage type (Settings -> Models).
                usage_details = {
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                }
                for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                    tokens = getattr(usage, key, 0) or 0
                    if tokens:
                        usage_details[key] = tokens
                cost_details = {k: v * _PRICE_PER_TOKEN[k]
                                for k, v in usage_details.items()}
                cost_details["total"] = sum(cost_details.values())
                gen.update(output=result.model_dump(),
                           usage_details=usage_details,
                           cost_details=cost_details)
            except Exception:
                pass
    return result
