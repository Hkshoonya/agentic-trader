"""TypeSafe Jev as the regime classifier.

Jev is a **judgment** model: you send state and typed questions and get back
calibrated answers with probabilities, not prose. That fits this system's regime
gate exactly, and it fixes a real cost: the gate currently asks a chat model for
one free-text read per symbol, which was 368 calls on a quiet day. Jev answers a
Choice question per symbol, and because independent questions run in parallel,
all sixteen symbols come back in **one** call — with a probability for every
regime instead of a single parsed label.

Everything here is bounded the same way the rest of the model layer is: a view
can only *block* an entry (chop/panic above the confidence floor). Nothing in
this module can create a trade, size one, or extend a hold.

Docs read for this integration (2026-09-18):
  https://docs.typesafe.ai/api.md            request/response contract
  https://docs.typesafe.ai/primitives/choice.md
  https://docs.typesafe.ai/confidence.md     confidence is derived from the
  distribution; low confidence means "do not act on this"
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx

from agentic_trading.llm.market import MarketFeatures, context_lines
from agentic_trading.llm.regime import REGIMES, RegimeGate, RegimeView

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
API_KEY_ENV = "TYPESAFE_API_KEY"
BASE_URL_ENV = "TYPESAFE_BASE_URL"
MODEL_ENV = "TYPESAFE_MODEL"

SYSTEM_PROMPT = (
    "You classify the market regime for a systematic trend follower. Read the "
    "numbers for one instrument and choose the regime that best describes the "
    "next few sessions: trend_up, trend_down, chop or panic. Use chop when "
    "direction is unclear, and panic only for disorderly, high-volatility "
    "declines. Judge only from the state given."
)


def build_questions(symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    """One Choice question per symbol, all asked in a single request.

    Question ids are for code only — the model never sees them — so the full
    meaning lives in ``instructions``, which names the symbol explicitly.
    """
    questions: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        questions[f"regime_{symbol.upper()}"] = {
            "type": "choice",
            "instructions": (
                f"Which regime describes {symbol.upper()} over the next few "
                "sessions, given the state?"
            ),
            "criteria": {
                "trend_up": "A sustained rise: the 20-bar slope and returns point up.",
                "trend_down": "A sustained fall: the 20-bar slope and returns point down.",
                "chop": "No dominant direction: range-bound or conflicting horizons.",
                "panic": "Disorderly, high-volatility decline; gaps and wide ranges.",
            },
        }
    return questions


def symbol_state(symbol: str, features: Optional[MarketFeatures]) -> dict[str, Any]:
    """The numbers the judgment is allowed to rest on, as named JSON fields."""
    state: dict[str, Any] = {"symbol": symbol.upper()}
    if features is None:
        state["note"] = "no market features available for this instrument"
        return state
    state.update(features.to_dict())
    return state


def parse_regime_answers(
    payload: Any,
    *,
    symbols: Iterable[str],
    features_by_symbol: Optional[dict[str, MarketFeatures]] = None,
    at: Optional[str] = None,
    model: str = "",
) -> dict[str, RegimeView]:
    """Turn Choice answers into the gate's own view type.

    Fail closed *per symbol*: an unparseable or unknown answer leaves that
    instrument without a view (which blocks nothing), rather than inventing a
    classification or discarding the whole batch.
    """
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        return {}
    features_by_symbol = features_by_symbol or {}
    stamp = at or datetime.now(timezone.utc).isoformat()
    views: dict[str, RegimeView] = {}
    for symbol in symbols:
        key = symbol.upper()
        answer = answers.get(f"regime_{key}")
        if not isinstance(answer, dict):
            continue
        regime = str(answer.get("choice", "")).strip().lower()
        if regime not in REGIMES:
            continue
        try:
            confidence = float(answer.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        probabilities = answer.get("probabilities")
        detail = ""
        if isinstance(probabilities, dict):
            parts = []
            for name, value in probabilities.items():
                try:
                    parts.append(f"{name} {float(value):.2f}")
                except (TypeError, ValueError):
                    continue
            detail = ", ".join(parts)
        features = features_by_symbol.get(key)
        views[key] = RegimeView(
            symbol=key,
            regime=regime,
            confidence=min(1.0, max(0.0, confidence)),
            reason=(
                f"{model or 'jev'}: {detail}" if detail else f"{model or 'jev'} choice"
            )[:200],
            at=stamp,
            market=features.to_dict() if features is not None else {},
        )
    return views


class JevClient:
    """``POST {base}/v1/systemone`` with a bearer key."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def system_one(
        self, *, state: Any, questions: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {"state": state, "model": self.model, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/systemone", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected TypeSafe response: not an object")
        return data


class JevRegimeGate(RegimeGate):
    """Regime gate that classifies every stale symbol in one Jev call."""

    def __init__(
        self,
        client: JevClient,
        *,
        ttl_seconds: float = 900.0,
        block_confidence: float = 0.6,
        clock: Any = None,
        state_path: Any = None,
    ) -> None:
        # The parent stores the client and the cached views; ``blocks``/``views``/
        # ``due``/``load``/``save`` are reused unchanged so the gate behaves
        # identically whichever backend produced a view.
        super().__init__(
            client,
            model=getattr(client, "model", ""),
            ttl_seconds=ttl_seconds,
            block_confidence=block_confidence,
            clock=clock,
            state_path=state_path,
        )

    def refresh(self, symbol: str, features: Optional[MarketFeatures]) -> Optional[RegimeView]:
        views = self.refresh_many({symbol.upper(): features})
        return views.get(symbol.upper())

    def refresh_many(
        self, features_by_symbol: dict[str, Optional[MarketFeatures]]
    ) -> dict[str, RegimeView]:
        """One request, one answer per symbol."""
        symbols = [symbol.upper() for symbol in features_by_symbol]
        if not symbols:
            return {}
        state = {
            "instruments": [
                symbol_state(symbol, features_by_symbol[symbol]) for symbol in symbols
            ],
            "task": SYSTEM_PROMPT,
        }
        try:
            payload = self.client.system_one(
                state=state, questions=build_questions(symbols)
            )
        except Exception as exc:  # noqa: BLE001 — a gate outage must never block
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return {}
        views = parse_regime_answers(
            payload,
            symbols=symbols,
            features_by_symbol={
                symbol: features
                for symbol, features in features_by_symbol.items()
                if features is not None
            },
            model=getattr(self.client, "model", ""),
        )
        if not views:
            self.errors += 1
            self.last_error = f"no usable answers in {str(payload)[:200]}"
            return {}
        self.last_error = ""
        self._views.update(views)
        self.save()
        return views

    def refresh_due(
        self,
        symbols: Iterable[str],
        features_for: Any,
        *,
        max_per_pass: int = 2,
    ) -> list[RegimeView]:
        """Refresh every stale symbol at once.

        ``max_per_pass`` bounds a chat-model gate because each symbol is a round
        trip. One Jev call covers the whole book, so the bound becomes the
        whitelist itself — the reason this backend exists.
        """
        due = [str(symbol).upper() for symbol in symbols if self.due(str(symbol))]
        if not due:
            return []
        features_by_symbol = {symbol: features_for(symbol) for symbol in due}
        views = self.refresh_many(features_by_symbol)
        return [views[symbol] for symbol in due if symbol in views]


def build_jev_gate(
    *,
    state_path: Any = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    ttl_seconds: float = 900.0,
) -> Optional[JevRegimeGate]:
    """The gate, or ``None`` when no TypeSafe key is configured."""
    from agentic_trading.llm.client import load_dotenv

    load_dotenv()
    key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
    if not key:
        return None
    client = JevClient(
        api_key=key,
        base_url=base_url
        or os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL),
        model=model or os.environ.get(MODEL_ENV, DEFAULT_MODEL),
    )
    return JevRegimeGate(client, ttl_seconds=ttl_seconds, state_path=state_path)
