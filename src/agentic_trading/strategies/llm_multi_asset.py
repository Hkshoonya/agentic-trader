"""Multi-asset LLM strategy: brief quote context → JSON intents → OrderIntent."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from agentic_trading.llm.client import LlmClient
from agentic_trading.types import OrderIntent, Side, new_decision_id

SYSTEM_PROMPT = (
    "You are a trading assistant for a shadow-first agentic system. "
    "Propose at most one equity order intent as JSON only, no markdown. "
    'Schema: {"intents":[{"symbol":"TICKER","side":"buy"|"sell",'
    '"quantity":"0.01","reason":"short reason"}]}. '
    "Use an empty intents array to propose nothing. "
    "Only propose symbols from the provided whitelist. "
    "This is research software — no profit guarantee."
)

DEFAULT_QUANTITY = Decimal("0.01")
DEFAULT_MAX_QUOTE_AGE_SECONDS = 60.0


def _parse_observed_at(quote: dict) -> datetime:
    value = quote.get("observed_at")
    if not isinstance(value, str):
        raise ValueError("missing observed_at")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


class LlmMultiAssetStrategy:
    """Ask an LlmClient for 0..1 intents; enforce whitelist; emit OrderIntents."""

    def __init__(
        self,
        client: LlmClient,
        whitelist: frozenset[str],
        *,
        default_quantity: Decimal = DEFAULT_QUANTITY,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.whitelist = frozenset(s.upper() for s in whitelist)
        self.default_quantity = Decimal(str(default_quantity))
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self.clock = clock

    def on_quote(self, quote: dict) -> list[OrderIntent]:
        try:
            symbol, bid, ask, observed, quoted = self._validate_quote(quote)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            return []

        if symbol not in self.whitelist:
            return []

        user = self._build_user_prompt(symbol=symbol, bid=bid, ask=ask)
        try:
            raw = self.client.complete(SYSTEM_PROMPT, user)
        except Exception:  # noqa: BLE001 — never crash the quote loop on LLM errors
            return []

        proposal = self._parse_proposal(raw)
        if proposal is None:
            return []

        proposed_symbol = str(proposal["symbol"]).upper()
        if proposed_symbol not in self.whitelist:
            return []

        side_raw = str(proposal["side"]).lower()
        if side_raw not in ("buy", "sell"):
            return []
        side = Side.BUY if side_raw == "buy" else Side.SELL

        try:
            quantity = Decimal(str(proposal["quantity"]))
        except (InvalidOperation, KeyError, TypeError):
            return []
        if quantity <= 0 or not quantity.is_finite():
            return []

        reason = str(proposal.get("reason") or "llm:propose")
        ref_price = ask if side is Side.BUY else bid

        return [
            OrderIntent(
                decision_id=new_decision_id(),
                symbol=proposed_symbol,
                side=side,
                quantity=quantity,
                ref_price=ref_price,
                reason=reason,
                created_at=observed,
            )
        ]

    def _validate_quote(
        self, quote: dict
    ) -> tuple[str, Decimal, Decimal, datetime, datetime]:
        if not isinstance(quote, dict):
            raise ValueError("quote must be an object")
        symbol = str(quote["symbol"]).upper()
        bid = Decimal(str(quote["bid"]))
        ask = Decimal(str(quote["ask"]))
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
            raise ValueError("invalid_or_crossed_prices")
        observed = _parse_observed_at(quote)
        quoted = _parse_observed_at({"observed_at": quote.get("quote_at")})
        age = (observed - quoted).total_seconds()
        if age < 0:
            raise ValueError("future_quote")
        # An LLM call costs seconds; never let a stale tick reach a real order.
        if age > self.max_quote_age_seconds:
            raise ValueError("stale_quote")
        return symbol, bid, ask, observed, quoted

    def _build_user_prompt(self, *, symbol: str, bid: Decimal, ask: Decimal) -> str:
        whitelist = ", ".join(sorted(self.whitelist)) or "(empty)"
        return (
            f"symbol={symbol} bid={bid} ask={ask}\n"
            f"whitelist=[{whitelist}]\n"
            "Respond with JSON only."
        )

    def _parse_proposal(self, raw: str) -> Optional[dict[str, Any]]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = raw.strip()
        # Tolerate optional markdown fences from chat models.
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
        intents = payload.get("intents")
        if not isinstance(intents, list) or not intents:
            return None
        first = intents[0]
        if not isinstance(first, dict):
            return None
        if "symbol" not in first or "side" not in first or "quantity" not in first:
            return None
        return first
