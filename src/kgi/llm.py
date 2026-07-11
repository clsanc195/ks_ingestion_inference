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
                gen.update(
                    output=result.model_dump(),
                    usage_details={
                        "input": completion.usage.input_tokens,
                        "output": completion.usage.output_tokens,
                    },
                )
            except Exception:
                pass
    return result
