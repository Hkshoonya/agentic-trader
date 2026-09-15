"""Read quote JSONL files (same format as paper_scalper)."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

_OPTIONAL_FIELDS = ("source", "session", "delayed")


def _parse_decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def _normalize_quote(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        symbol = str(raw["symbol"]).upper()
        observed_at = str(raw["observed_at"])
        quote_at = str(raw["quote_at"])
        bid = _parse_decimal(raw["bid"])
        ask = _parse_decimal(raw["ask"])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if bid <= 0 or ask < bid:
        return None

    quote: dict[str, Any] = {
        "symbol": symbol,
        "observed_at": observed_at,
        "quote_at": quote_at,
        "bid": bid,
        "ask": ask,
    }
    for key in _OPTIONAL_FIELDS:
        if key in raw:
            quote[key] = raw[key]
    return quote


def iter_quotes(path: Path | str) -> Iterator[dict]:
    """Yield normalized quotes from a JSONL file, skipping malformed lines."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            quote = _normalize_quote(raw)
            if quote is not None:
                yield quote
