"""New recipes from old ones: mutation, crossover and random immigrants.

Every choice comes from the recipe's fixed lists, so a child is always a
valid recipe. The caller seeds the RNG (P5), so a step can be replayed.
"""

from __future__ import annotations

import random
from typing import Any

from agentic_trading.swarm.recipe import CHOICES, FAMILIES, SIZING, UNIVERSES, Recipe, validate


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _universes(family: str) -> list[str]:
    return [u for u in UNIVERSES if not (family == "reversal" and u == "crypto")]


def immigrant(rng: random.Random) -> Recipe:
    family = rng.choice(FAMILIES)
    raw = {"family": family,
           "params": {key: _plain(rng.choice(options)) for key, options in CHOICES[family].items()},
           "universe": rng.choice(_universes(family)),
           "per_order_pct": rng.choice(SIZING["per_order_pct"]),
           "inverse_vol": rng.choice(SIZING["inverse_vol"])}
    return validate(raw, origin="immigrant", parents=())


def mutate(parent: Recipe, rng: random.Random) -> Recipe:
    raw = parent.content()
    genes = [("params", key) for key in CHOICES[parent.family]] + [
        ("top", "per_order_pct"), ("top", "inverse_vol"), ("top", "universe")]
    for where, key in rng.sample(genes, k=rng.choice((1, 2))):
        if where == "params":
            options = [_plain(o) for o in CHOICES[parent.family][key]]
            current = raw["params"][key]
            raw["params"][key] = rng.choice([o for o in options if o != current])
        else:
            options = _universes(parent.family) if key == "universe" else list(SIZING[key])
            raw[key] = rng.choice([o for o in options if o != raw[key]])
    return validate(raw, origin="mutation", parents=(parent.id,))


def crossover(a: Recipe, b: Recipe, rng: random.Random) -> Recipe:
    if a.family != b.family:
        return mutate(a, rng)
    left, right = a.content(), b.content()
    raw = {"family": a.family,
           "params": {key: rng.choice((left["params"][key], right["params"][key])) for key in left["params"]},
           "universe": rng.choice((left["universe"], right["universe"])),
           "per_order_pct": rng.choice((left["per_order_pct"], right["per_order_pct"])),
           "inverse_vol": rng.choice((left["inverse_vol"], right["inverse_vol"]))}
    return validate(raw, origin="crossover", parents=(a.id, b.id))
