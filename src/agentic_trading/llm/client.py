"""LLM client Protocol, Fake, and optional OpenAI-compatible HTTP."""

from __future__ import annotations

import os
from typing import Callable, Optional, Protocol, Union

import httpx

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_FAKE_RESPONSE = '{"intents":[]}'


class LlmClient(Protocol):
    """Minimal chat-completion interface returning raw text (JSON proposals)."""

    def complete(self, system: str, user: str) -> str: ...


class FakeLlmClient:
    """Deterministic stand-in for tests and CLI when no API key is set."""

    def __init__(
        self,
        response: Union[str, Callable[[str, str], str]] = DEFAULT_FAKE_RESPONSE,
    ) -> None:
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if callable(self._response):
            return self._response(system, user)
        return self._response


class OpenAiCompatibleLlmClient:
    """POST ``{base}/chat/completions`` (OpenAI-compatible HTTP)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected LLM response shape: {exc}") from exc
        if not isinstance(content, str):
            raise ValueError("LLM content must be a string")
        return content


def build_llm_client(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> LlmClient:
    """Build OpenAI-compatible client when key present; else Fake with empty intents."""
    key = api_key if api_key is not None else os.environ.get("AGENTIC_LLM_API_KEY")
    if not key:
        return FakeLlmClient(DEFAULT_FAKE_RESPONSE)
    return OpenAiCompatibleLlmClient(
        base_url=(
            base_url
            if base_url is not None
            else os.environ.get("AGENTIC_LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
        ),
        api_key=key,
        model=(
            model
            if model is not None
            else os.environ.get("AGENTIC_LLM_MODEL", DEFAULT_LLM_MODEL)
        ),
    )
