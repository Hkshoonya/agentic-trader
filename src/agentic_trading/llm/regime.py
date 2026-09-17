"""LLM market-regime classification, used as a risk-reducing gate.

The advisor judges one order at a time. This judges the *context* those orders
arrive in, and it is deliberately limited to one direction: a bad regime can
stop entries, and a good regime can never start one. The mechanical strategy
still has to produce the signal, and RiskGuard still has to allow it.

Two design choices keep it from becoming a latency or reliability problem:

- classifications are cached per symbol for ``ttl_seconds`` (default 15 min) and
  refreshed by the off-path evaluation worker, so the order path only ever reads
  a cached view and never waits on a model call;
- a missing, stale, or unparseable classification means "no opinion", which
  allows trading exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from agentic_trading.llm.client import LlmClient
from agentic_trading.llm.market import MarketFeatures, context_lines

REGIMES = ("trend_up", "trend_down", "chop", "panic")

SYSTEM_PROMPT = (
    "You classify the market regime for an automated trading agent. You do not "
    "place or size trades. Answer with JSON only, no markdown. "
    'Schema: {"regime":"trend_up"|"trend_down"|"chop"|"panic",'
    '"confidence":0.0-1.0,"reason":"short reason"}. '
    "Use 'trend_up'/'trend_down' only when a directional trend dominates the "
    "window. Use 'chop' when price is range-bound or signals conflict, and "
    "'panic' for disorderly, high-volatility moves. Judge only the numbers "
    "given. No profit guarantee."
)

# A regime may only block entries when the model is reasonably sure.
DEFAULT_BLOCK_CONFIDENCE = 0.6
BLOCKING_REGIMES = ("chop", "panic")


@dataclass(frozen=True)
class RegimeView:
    symbol: str
    regime: str
    confidence: float
    reason: str
    at: str
    market: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_entries(self) -> bool:
        return self.regime in BLOCKING_REGIMES

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "regime": self.regime,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "at": self.at,
            "blocks_entries": self.blocks_entries,
            "market": dict(self.market),
        }


def parse_regime(raw: str, *, symbol: str, at: str = "") -> Optional[RegimeView]:
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
    regime = str(payload.get("regime", "")).strip().lower()
    if regime not in REGIMES:
        return None
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return RegimeView(
        symbol=symbol,
        regime=regime,
        confidence=min(1.0, max(0.0, confidence)),
        reason=str(payload.get("reason", ""))[:200],
        at=at or datetime.now(timezone.utc).isoformat(),
    )


class RegimeGate:
    """Cached per-symbol regime views; the order path only ever reads them."""

    def __init__(
        self,
        client: LlmClient,
        *,
        model: str = "",
        ttl_seconds: float = 900.0,
        block_confidence: float = DEFAULT_BLOCK_CONFIDENCE,
        clock: Any = None,
        state_path: Any = None,
    ) -> None:
        self.client = client
        self.model = model
        self.ttl_seconds = ttl_seconds
        self.block_confidence = block_confidence
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.state_path = Path(state_path) if state_path else None
        self._views: dict[str, RegimeView] = {}
        self.errors = 0
        self.last_error = ""
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> int:
        """Restore classifications written by a previous run.

        Without this a restart makes the gate deaf until the worker catches up,
        and the bot quietly forgets what it had learned about the market.
        """
        if self.state_path is None or not self.state_path.is_file():
            return 0
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        views = payload.get("views") if isinstance(payload, dict) else None
        if not isinstance(views, dict):
            return 0
        restored = 0
        for symbol, raw in views.items():
            if not isinstance(raw, dict):
                continue
            regime = str(raw.get("regime", ""))
            if regime not in REGIMES:
                continue
            try:
                confidence = float(raw.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            self._views[str(symbol).upper()] = RegimeView(
                symbol=str(symbol).upper(),
                regime=regime,
                confidence=min(1.0, max(0.0, confidence)),
                reason=str(raw.get("reason", ""))[:200],
                at=str(raw.get("at") or datetime.now(timezone.utc).isoformat()),
            )
            restored += 1
        return restored

    def save(self) -> None:
        if self.state_path is None:
            return
        from agentic_trading import jsonio

        try:
            jsonio.write_text(
                self.state_path,
                jsonio.dumps(
                    {
                        "model": self.model,
                        "updated_at": self._clock().isoformat(),
                        "views": self.views(),
                    },
                    indent=2,
                )
                + "\n",
            )
        except OSError:
            return

    # -- reads (never touch the network) ---------------------------------

    def view(self, symbol: str) -> Optional[RegimeView]:
        return self._views.get(symbol.upper())

    def blocks(self, symbol: str) -> Optional[RegimeView]:
        """The view that should stop an entry, or ``None`` to let it through."""
        view = self.view(symbol)
        if view is None or not view.blocks_entries:
            return None
        if view.confidence < self.block_confidence:
            return None
        return view

    def due(self, symbol: str) -> bool:
        view = self.view(symbol)
        if view is None:
            return True
        try:
            seen = datetime.fromisoformat(view.at)
        except ValueError:
            return True
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = (self._clock() - seen).total_seconds()
        return age >= self.ttl_seconds

    def views(self) -> dict[str, dict[str, Any]]:
        return {symbol: view.to_dict() for symbol, view in self._views.items()}

    # -- refresh (worker thread only) -------------------------------------

    def refresh(
        self, symbol: str, features: Optional[MarketFeatures]
    ) -> Optional[RegimeView]:
        prompt_lines = [f"symbol={symbol}"]
        prompt_lines.extend(context_lines(features))
        prompt_lines.append("Respond with JSON only.")
        try:
            raw = self.client.complete(SYSTEM_PROMPT, "\n".join(prompt_lines))
        except Exception as exc:  # noqa: BLE001 — a gate outage must never block
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            return None
        view = parse_regime(raw, symbol=symbol.upper())
        if view is None:
            self.errors += 1
            self.last_error = f"unparseable regime output: {raw[:200]!r}"
            return None
        if features is not None:
            # Record the numbers the classification was based on.
            view = RegimeView(
                symbol=view.symbol,
                regime=view.regime,
                confidence=view.confidence,
                reason=view.reason,
                at=view.at,
                market=features.to_dict(),
            )
        self.last_error = ""
        self._views[view.symbol] = view
        self.save()
        return view

    def refresh_due(
        self,
        symbols: Iterable[str],
        features_for: Any,
        *,
        max_per_pass: int = 2,
    ) -> list[RegimeView]:
        """Refresh a bounded number of stale symbols (one model call each)."""
        refreshed: list[RegimeView] = []
        for symbol in symbols:
            if len(refreshed) >= max_per_pass:
                break
            if not self.due(symbol):
                continue
            features = features_for(symbol)
            view = self.refresh(symbol, features)
            if view is not None:
                refreshed.append(view)
        return refreshed
