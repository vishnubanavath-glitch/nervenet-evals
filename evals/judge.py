"""The judge LLM that scores answers (Claude Opus by default, set by JUDGE_MODEL)."""
from __future__ import annotations

import os
from functools import lru_cache

from deepeval.models import AnthropicModel

# Models under test; the judge must not be one of them (a model grading itself is lenient).
MODELS_UNDER_TEST = {"claude-sonnet-5-5", "claude-haiku-5-5"}


@lru_cache(maxsize=1)
def get_judge() -> AnthropicModel:
    model = os.getenv("JUDGE_MODEL", "claude-opus-5-5")
    if model in MODELS_UNDER_TEST:
        raise SystemExit(f"JUDGE_MODEL={model} is a model under test; use a different judge.")
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set (eval .env or mcp-utilities/.env).")
    return AnthropicModel(model=model, api_key=key)
