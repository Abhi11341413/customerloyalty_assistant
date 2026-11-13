"""One LLM interface for the whole project.

- AnthropicLLM: Claude via the official SDK, structured output validated against DraftOutput.
- FakeLLM: deterministic stand-in for tests and offline runs (never calls the network).
- get_llm(): Anthropic when a key is configured (.env: ANTHROPIC_API_KEY or LLM_API_KEY),
  otherwise None -> the writer uses the approved template (fallback=True).
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Protocol

from src.config import policy
from src.schemas import DraftOutput


class LLMError(RuntimeError):
    """Any failure to get a usable answer (network, timeout, refusal, empty output)."""


class LLMClient(Protocol):
    model_id: str

    def complete(self, system: str, user: str) -> str:
        """Return the model's answer as a JSON string."""
        ...


class AnthropicLLM:
    def __init__(self, api_key: str | None = None, model_id: str | None = None):
        import anthropic
        g = policy().generation
        self.model_id = model_id or g.model_id
        self._effort = g.effort
        self._client = anthropic.Anthropic(api_key=api_key, timeout=float(g.timeout_s), max_retries=1)

    def complete(self, system: str, user: str) -> str:
        import anthropic
        try:
            resp = self._client.messages.parse(
                model=self.model_id,
                max_tokens=4000,
                system=system,
                output_config={"effort": self._effort},
                messages=[{"role": "user", "content": user}],
                output_format=DraftOutput,
            )
        except anthropic.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("rate limited") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if resp.parsed_output is None:
            raise LLMError(f"no structured output (stop_reason={resp.stop_reason})")
        return resp.parsed_output.model_dump_json()


class FakeLLM:
    """Test double. `responses` is a list of strings returned in order (the last repeats),
    a callable (system, user) -> str, or None to echo the approved example from the prompt."""

    model_id = "fake-llm"

    def __init__(self, responses: list[str] | Callable[[str, str], str] | None = None,
                 raise_error: Exception | None = None):
        self.responses = responses
        self.raise_error = raise_error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.raise_error:
            raise self.raise_error
        if callable(self.responses):
            return self.responses(system, user)
        if self.responses:
            return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        m = re.search(r"<approved_example>\s*(\{.*?\})\s*</approved_example>", user, re.S)
        if not m:
            raise LLMError("no example in prompt")
        return m.group(1)


def get_llm() -> LLMClient | None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
    if not key:
        return None
    return AnthropicLLM(api_key=key)


def example_json(subject: str, body: str, claims: list[dict], cta: str) -> str:
    return json.dumps({"subject": subject, "body": body, "claims": claims, "cta": cta}, indent=1)
