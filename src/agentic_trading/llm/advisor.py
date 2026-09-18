"""LLM as an advisory layer over the mechanical strategy.

Deliberate asymmetry, because a model cannot be backtested the way a genome can:

- it **may veto** an entry — refusing to take a trade can only reduce risk
- it **may not** extend a hold in this version. A hold opinion is recorded with
  its reasoning so it can be scored against what actually happened, but it does
  not override a mechanical exit. Extending risk on unvalidated model output is
  how a stop-loss quietly stops working.

An advisor failure (no key, bad JSON, HTTP error) is never fatal and never
blocks trading: RiskGuard remains the safety layer, and the advisor is a filter
on top of it. Every decision is journaled for later evaluation.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agentic_trading.llm.client import LlmClient, build_llm_client

SYSTEM_PROMPT = (
    "You are a risk gate for an automated trading agent. You may only VETO a "
    "trade; you can never create one. Answer with JSON only, no markdown. "
    'Schema: {"action":"allow"|"veto","confidence":0.0-1.0,"hold":true|false,'
    '"reason":"short reason"}. '
    "Veto when the setup looks like a trap (blow-off move, dead liquidity, "
    "earnings/news risk, counter-trend chase). Otherwise allow. "
    "'hold' is your opinion on whether an existing position should keep running; "
    "it is recorded but not acted on. No profit guarantee."
)


@dataclass(frozen=True)
class AdvisorDecision:
    action: str  # allow | veto
    confidence: float
    hold: bool
    reason: str

    @property
    def vetoes(self) -> bool:
        return self.action == "veto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "confidence": round(self.confidence, 3),
            "hold": self.hold,
            "reason": self.reason,
        }


def advisor_enabled() -> bool:
    """Opt-in per session: AGENTIC_LLM_ADVISOR=1 (shell export or .env)."""
    from agentic_trading.llm.client import load_dotenv

    load_dotenv()  # so a .env-only setting still enables the advisor
    return os.environ.get("AGENTIC_LLM_ADVISOR") == "1"


def build_regime_gate(
    client: Optional[LlmClient] = None,
    *,
    model: str = "",
    state_path: Any = None,
) -> Optional["RegimeGate"]:
    """Regime gate on the same opt-in as the advisor; ``None`` when disabled.

    ``AGENTIC_REGIME_BACKEND=jev`` swaps the chat-model classifier for TypeSafe's
    Jev, which answers a Choice question per symbol and therefore classifies the
    whole book in one call with a probability per regime. The gate's behaviour is
    unchanged: a view may only block an entry, never create one.
    """
    from agentic_trading.llm.regime import RegimeGate
    from agentic_trading.llm.client import FakeLlmClient

    if not advisor_enabled():
        return None
    # `AGENTIC_LLM_REGIME_BACKEND` is a natural guess and was typed by hand once
    # already; accept it rather than making the operator read the source.
    backend = (
        os.environ.get("AGENTIC_REGIME_BACKEND")
        or os.environ.get("AGENTIC_LLM_REGIME_BACKEND")
        or ""
    ).strip().lower()
    if backend == "jev":
        from agentic_trading.llm.jev import build_jev_gate

        gate = build_jev_gate(state_path=state_path)
        if gate is not None:
            return gate
        # Configured for Jev without a key: say so in the log rather than
        # silently falling back to a different model's judgments.
        import logging

        logging.getLogger("agentic_trading.llm").warning(
            "AGENTIC_REGIME_BACKEND=jev but %s is unset; using the chat model "
            "regime gate instead",
            "TYPESAFE_API_KEY",
        )
    resolved = client if client is not None else build_llm_client()
    if isinstance(resolved, FakeLlmClient):
        return None
    return RegimeGate(
        resolved,
        model=model or getattr(resolved, "model", ""),
        state_path=state_path,
    )


class LlmAdvisor:
    def __init__(
        self,
        client: LlmClient,
        *,
        model: str = "",
        cache_seconds: float = 300.0,
        max_calls_per_minute: int = 20,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.client = client
        self.model = model
        self.decisions: list[AdvisorDecision] = []
        self.errors = 0
        self.last_error: str = ""
        self.last_reused: bool = False
        self.cache_seconds = cache_seconds
        self.max_calls_per_minute = max_calls_per_minute
        self.cache_hits = 0
        self.cache_misses = 0
        self.budget_skips = 0
        self._clock = clock or time.monotonic
        self._cache: dict[str, tuple[float, AdvisorDecision]] = {}
        self._call_times: list[float] = []

    # -- burst control -----------------------------------------------------

    @staticmethod
    def situation_key(symbol: str, side: str, features: Any) -> str:
        """Identify the *situation*, not just the symbol.

        A veto is about a setup: "counter-trend bounce into a range high on
        thin volume". Two intents with the same symbol, side and coarse market
        state are the same question, and re-asking costs ~4s inside the decision
        path. Coarse buckets are deliberate — the key must not be so fine that
        it never matches, nor so loose that different setups share a verdict.
        """
        if features is None:
            return f"{symbol}:{side}:nofeatures"
        bucket = (
            round(getattr(features, "trend_pct", 0.0) / 2.0),
            round(getattr(features, "range_position", 0.5) * 4),
            round(getattr(features, "vol_pct", 0.0)),
            round((getattr(features, "spread_bps", 0.0) or 0.0) / 5.0),
        )
        return f"{symbol}:{side}:{bucket}"

    def _within_budget(self, now: float) -> bool:
        if self.max_calls_per_minute <= 0:
            return True
        cutoff = now - 60.0
        self._call_times = [stamp for stamp in self._call_times if stamp >= cutoff]
        return len(self._call_times) < self.max_calls_per_minute

    def cached(self, key: str, *, now: Optional[float] = None) -> Optional[AdvisorDecision]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stamp, decision = entry
        moment = self._clock() if now is None else now
        if self.cache_seconds > 0 and (moment - stamp) > self.cache_seconds:
            del self._cache[key]
            return None
        return decision

    def review_entry(
        self,
        *,
        symbol: str,
        side: str,
        ref_price: str,
        quantity: str,
        reason: str,
        context: Optional[dict[str, Any]] = None,
    ) -> Optional[AdvisorDecision]:
        """Return a decision, or ``None`` when the advisor has no usable opinion."""
        features = (context or {}).get("market")
        key = self.situation_key(symbol.upper(), side, features)
        self.last_reused = False
        now = self._clock()

        # Same question, asked again a few seconds later: reuse the verdict
        # instead of paying another model round trip inside the order path.
        reused = self.cached(key, now=now)
        if reused is not None:
            self.cache_hits += 1
            self.last_reused = True
            return reused

        if not self._within_budget(now):
            # The advisor is an optional filter; the mandatory gates (RiskGuard,
            # regime, correlation) still apply. Degrade to "no opinion" and say
            # so, rather than stalling the loop behind a queue of model calls.
            self.budget_skips += 1
            self.last_error = "advisor budget exhausted; no opinion this cycle"
            return None

        prompt_lines = [
            f"symbol={symbol}",
            f"proposed={side} quantity={quantity} at {ref_price}",
            f"strategy_reason={reason}",
        ]
        # NB: do not reuse ``key`` here — it holds the cache key for this
        # situation, and shadowing it silently cached every verdict under the
        # name of the last prompt field.
        for name, value in (context or {}).items():
            if name == "market":
                continue
            prompt_lines.append(f"{name}={value}")
        # Ground the judgment in the tape rather than a bare price.
        from agentic_trading.llm.market import context_lines

        prompt_lines.extend(context_lines(features))
        prompt_lines.append("Respond with JSON only.")
        try:
            raw = self.client.complete(SYSTEM_PROMPT, "\n".join(prompt_lines))
        except Exception as exc:  # noqa: BLE001 — advisor outages must not stop trading
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return None
        decision = parse_decision(raw)
        if decision is None:
            self.errors += 1
            self.last_error = f"unparseable model output: {raw[:200]!r}"
            return None
        self.last_error = ""
        self.cache_misses += 1
        self._call_times.append(now)
        if self.cache_seconds > 0:
            self._cache[key] = (now, decision)
        self.decisions.append(decision)
        return decision


def parse_decision(raw: str) -> Optional[AdvisorDecision]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action", "")).lower()
    if action not in ("allow", "veto"):
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    return AdvisorDecision(
        action=action,
        confidence=confidence,
        hold=bool(payload.get("hold", False)),
        reason=str(payload.get("reason", ""))[:200],
    )


def build_advisor() -> Optional[LlmAdvisor]:
    """Build the advisor when enabled and a real client is configured."""
    if not advisor_enabled():
        return None
    from agentic_trading.llm.client import FakeLlmClient

    client = build_llm_client()
    if isinstance(client, FakeLlmClient):
        return None
    def _float(name: str, fallback: float) -> float:
        try:
            return float(os.environ.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    return LlmAdvisor(
        client,
        model=os.environ.get("AGENTIC_LLM_MODEL", ""),
        cache_seconds=_float("AGENTIC_ADVISOR_CACHE_SECONDS", 300.0),
        max_calls_per_minute=int(_float("AGENTIC_ADVISOR_MAX_CALLS_PER_MINUTE", 20)),
    )
