"""A recipe: one daily-bar rule, written as data and never as code.

A recipe names a family the walk-forward already trades (trend, rotation or
reversal), that family's parameters chosen from fixed lists, which symbols it may
hold, and how big each position is. Its id is a hash of exactly that content,
so the same idea always has the same id, however it was found.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable

from agentic_trading.history import Bar
from agentic_trading.orders import is_crypto_symbol

FAMILIES = ("trend", "rotation", "reversal")
UNIVERSES = ("crypto", "equity", "all")
HORIZON_SETS: tuple[tuple[int, ...], ...] = ((10, 20, 50, 100), (20, 50, 100, 200), (50, 100, 200, 252),
                                             (100, 200, 252, 300))
POSITIONS = (1, 2, 3, 4, 5, 6, 8)
CHOICES: dict[str, dict[str, tuple[Any, ...]]] = {
    "trend": {"horizons": HORIZON_SETS, "min_vote": (0.5, 0.75, 1.0), "max_positions": POSITIONS},
    "rotation": {"equity_ma": (50, 100, 150, 200), "equity_lookback": (20, 40, 63, 90, 126),
                 "equity_top": (1, 2, 3, 4, 5), "crypto_ma": (50, 100, 150, 200),
                 "crypto_lookback": (20, 30, 60, 90), "crypto_top": (1, 2, 3, 4), "max_positions": POSITIONS},
    "reversal": {"regime_ma": (100, 150, 200), "trend_ma": (100, 150, 200), "lookback": (3, 5, 10),
                 "top": (1, 2, 3, 4, 5), "max_positions": (1, 2, 3, 4, 5)},
}
SIZING: dict[str, tuple[Any, ...]] = {"per_order_pct": (0.1, 0.15, 0.2, 0.25, 0.33), "inverse_vol": (False, True)}
ORIGINS = ("immigrant", "mutation", "crossover", "scout")


@dataclass(frozen=True)
class Recipe:
    family: str
    params: tuple[tuple[str, Any], ...]  # sorted (key, value) pairs; tuples, so hashable
    universe: str
    per_order_pct: float
    inverse_vol: bool
    origin: str = "immigrant"
    parents: tuple[str, ...] = ()

    def param(self, key: str) -> Any:
        return dict(self.params)[key]

    def content(self) -> dict[str, Any]:
        return {"family": self.family, "params": {k: list(v) if isinstance(v, tuple) else v for k, v in self.params},
                "universe": self.universe, "per_order_pct": self.per_order_pct, "inverse_vol": self.inverse_vol}

    @property
    def id(self) -> str:
        text = json.dumps(self.content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    @property
    def name(self) -> str:
        return f"{self.family}-{self.id[:4]}"

    def to_dict(self) -> dict[str, Any]:
        return {**self.content(), "origin": self.origin, "parents": list(self.parents)}

    def replace(self, **changes: Any) -> "Recipe":
        return replace(self, **changes)


def _whole(value: Any) -> Any:
    """``10.0`` is ``10``: JSON and LLMs write whole numbers as floats; the rankers index with them."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _choice(name: str, value: Any, allowed: tuple[Any, ...]) -> Any:
    if isinstance(value, list):
        value = tuple(value)
    if isinstance(value, tuple):
        value = tuple(_whole(v) for v in value)
    elif not any(isinstance(a, float) for a in allowed):
        value = _whole(value)
    kinds = {type(a) for a in allowed}
    if (isinstance(value, bool) != any(isinstance(a, bool) for a in allowed) or type(value) not in kinds
            or value not in allowed):
        shown = [list(a) if isinstance(a, tuple) else a for a in allowed]
        raise ValueError(f"{name} must be one of {shown}, not {value!r}")
    return value


def validate(raw: Any, *, origin: str | None = None, parents: Iterable[str] | None = None) -> Recipe:
    if not isinstance(raw, dict):
        raise ValueError("a recipe must be an object")
    family = raw.get("family")
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {list(FAMILIES)}, not {family!r}")
    universe = raw.get("universe")
    if universe not in UNIVERSES:
        raise ValueError(f"universe must be one of {list(UNIVERSES)}, not {universe!r}")
    if family == "reversal" and universe == "crypto":
        raise ValueError("reversal trades equities only; its universe cannot be crypto")
    params = raw.get("params")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    allowed = CHOICES[family]
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise ValueError(f"unknown params {unknown} for {family}")
    missing = sorted(set(allowed) - set(params))
    if missing:
        raise ValueError(f"missing params {missing} for {family}")
    cleaned = tuple(sorted((key, _choice(key, params[key], allowed[key])) for key in allowed))
    per_order_pct = _choice("per_order_pct", raw.get("per_order_pct"), SIZING["per_order_pct"])
    inverse_vol = _choice("inverse_vol", raw.get("inverse_vol"), SIZING["inverse_vol"])
    chosen_origin = origin or str(raw.get("origin") or "immigrant")
    if chosen_origin not in ORIGINS:
        raise ValueError(f"origin must be one of {list(ORIGINS)}")
    chosen_parents = tuple(str(p) for p in (parents if parents is not None else raw.get("parents") or ()))
    return Recipe(family, cleaned, universe, float(per_order_pct), bool(inverse_vol), chosen_origin, chosen_parents)


def rule_kwargs(recipe: Recipe) -> dict[str, Any]:
    """Arguments for ``targets_as_of``/``rank_targets``: which rule, with which parameters."""
    p = dict(recipe.params)
    if recipe.family == "trend":
        return {"rule": "trend", "horizons": tuple(p["horizons"]), "min_vote": p["min_vote"],
                "max_positions": p["max_positions"]}
    if recipe.family == "rotation":
        return {"rule": "rotation", "max_positions": p["max_positions"],
                "books": {"equity": ("SPY", p["equity_ma"], p["equity_lookback"], p["equity_top"]),
                          "crypto": ("BTCUSD", p["crypto_ma"], p["crypto_lookback"], p["crypto_top"])}}
    return {"rule": "reversal", "max_positions": p["max_positions"],
            "reversal": ("SPY", p["regime_ma"], p["trend_ma"], p["lookback"], p["top"])}


def sim_kwargs(recipe: Recipe) -> dict[str, Any]:
    """Arguments for ``simulate``: the rule plus its sizing (P4)."""
    return {**rule_kwargs(recipe), "per_order_pct": recipe.per_order_pct,
            "max_position_pct": recipe.per_order_pct, "inverse_vol": recipe.inverse_vol, "max_gross": 1.0}


def universe_series(series: dict[str, list[Bar]], universe: str) -> dict[str, list[Bar]]:
    if universe == "all":
        return dict(series)
    crypto = universe == "crypto"
    return {s: bars for s, bars in series.items() if is_crypto_symbol(s) == crypto}
