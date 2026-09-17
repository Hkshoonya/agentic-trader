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
from dataclasses import dataclass
from typing import Any, Optional

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
    """Regime gate on the same opt-in as the advisor; ``None`` when disabled."""
    from agentic_trading.llm.regime import RegimeGate
    from agentic_trading.llm.client import FakeLlmClient

    if not advisor_enabled():
        return None
    resolved = client if client is not None else build_llm_client()
    if isinstance(resolved, FakeLlmClient):
        return None
    return RegimeGate(
        resolved,
        model=model or getattr(resolved, "model", ""),
        state_path=state_path,
    )


class LlmAdvisor:
    def __init__(self, client: LlmClient, *, model: str = "") -> None:
        self.client = client
        self.model = model
        self.decisions: list[AdvisorDecision] = []
        self.errors = 0
        self.last_error: str = ""

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
        prompt_lines = [
            f"symbol={symbol}",
            f"proposed={side} quantity={quantity} at {ref_price}",
            f"strategy_reason={reason}",
        ]
        features = (context or {}).get("market")
        for key, value in (context or {}).items():
            if key == "market":
                continue
            prompt_lines.append(f"{key}={value}")
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
    return LlmAdvisor(client, model=os.environ.get("AGENTIC_LLM_MODEL", ""))
