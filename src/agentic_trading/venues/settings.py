"""The ``[venues]`` table of ``agentic.toml``.

Everything is off unless ``enabled = true``. A misspelled key is an error: a
silently ignored ``max_daily_loss_usd`` typo would leave the default cap in
force without anyone knowing.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VENUE_NAMES = ("alpaca_paper", "alpaca_live", "coinbase")
_STOCK = re.compile(r"^[A-Z][A-Z.]{0,9}$")
_ALPACA_CRYPTO = re.compile(r"^[A-Z]{2,10}/USD$")
_COINBASE = re.compile(r"^[A-Z]{2,10}-USD$")


@dataclass(frozen=True)
class VenuesConfig:
    enabled: bool = False
    alpaca_feed: str = "iex"
    alpaca_stock_symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA")
    alpaca_crypto_symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")
    coinbase_products: tuple[str, ...] = ("BTC-USD", "ETH-USD", "SOL-USD")
    use_alpaca_paper: bool = True
    use_alpaca_live: bool = False
    use_coinbase: bool = False
    stale_after_seconds: float = 30.0
    alpaca_paper_max_order_usd: Decimal = Decimal("500")
    alpaca_live_max_order_usd: Decimal = Decimal("10")
    coinbase_max_order_usd: Decimal = Decimal("10")
    max_daily_notional_usd: Decimal = Decimal("200")
    max_daily_loss_usd: Decimal = Decimal("10")
    max_open_orders: int = 5
    secrets_path: Path = Path("config/secrets.toml")
    stream_dir: Path = Path("data/stream")

    def max_order_usd(self, venue: str) -> Decimal:
        caps = {
            "alpaca_paper": self.alpaca_paper_max_order_usd,
            "alpaca_live": self.alpaca_live_max_order_usd,
            "coinbase": self.coinbase_max_order_usd,
        }
        return caps[venue]


def _money(name: str, value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"venues.{name} must be a number") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"venues.{name} must be positive")
    return amount


def _symbols(name: str, value: Any, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"venues.{name} must be a list")
    symbols = tuple(str(item).strip().upper() for item in value)
    bad = [s for s in symbols if not pattern.match(s)]
    if bad:
        raise ValueError(f"venues.{name} has symbols in the wrong format: {bad}")
    return symbols


def load_venues_config(path: Path | str) -> VenuesConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("venues") or {}
    if not isinstance(raw, dict):
        raise ValueError("[venues] must be a table")
    known = {f.name for f in fields(VenuesConfig)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"[venues] has unknown keys {unknown}; known: {sorted(known)}")
    values: dict[str, Any] = {}
    for key in ("enabled", "use_alpaca_paper", "use_alpaca_live", "use_coinbase"):
        if key in raw:
            values[key] = bool(raw[key])
    if "alpaca_feed" in raw:
        feed = str(raw["alpaca_feed"]).lower()
        if feed not in ("iex", "sip"):
            raise ValueError("venues.alpaca_feed must be iex or sip")
        values["alpaca_feed"] = feed
    for key, pattern in (
        ("alpaca_stock_symbols", _STOCK),
        ("alpaca_crypto_symbols", _ALPACA_CRYPTO),
        ("coinbase_products", _COINBASE),
    ):
        if key in raw:
            values[key] = _symbols(key, raw[key], pattern)
    for key in (
        "alpaca_paper_max_order_usd",
        "alpaca_live_max_order_usd",
        "coinbase_max_order_usd",
        "max_daily_notional_usd",
        "max_daily_loss_usd",
    ):
        if key in raw:
            values[key] = _money(key, raw[key])
    if "max_open_orders" in raw:
        count = int(raw["max_open_orders"])
        if count < 1:
            raise ValueError("venues.max_open_orders must be at least 1")
        values["max_open_orders"] = count
    if "stale_after_seconds" in raw:
        stale = float(raw["stale_after_seconds"])
        if stale <= 0:
            raise ValueError("venues.stale_after_seconds must be positive")
        values["stale_after_seconds"] = stale
    for key in ("secrets_path", "stream_dir"):
        if key in raw:
            values[key] = Path(str(raw[key]))
    return VenuesConfig(**values)
