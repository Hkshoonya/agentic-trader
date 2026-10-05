"""The LLM scout: an optional source of new recipes, and a story worth reading.

It is shown the recipe schema, the living agents and how many recipes have
been tried. It answers with one recipe and one sentence of reasoning. The
schema decides: anything that doesn't validate is refused. Each call costs one
of the week's proposals, whatever comes back. The scout never decides
anything else. Its recipes face the same screen, nursery and cull as every
other.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from agentic_trading.swarm.recipe import CHOICES, FAMILIES, SIZING, UNIVERSES, Recipe, validate

TIMEOUT = 15.0


def _shown(options: tuple[Any, ...]) -> list[Any]:
    return [list(o) if isinstance(o, tuple) else o for o in options]


SYSTEM = (
    "You propose ONE daily-bar trading recipe for a paper-trading swarm. Reply with JSON only, shaped "
    '{"recipe": {"family": ..., "params": {...}, "universe": ..., "per_order_pct": ..., "inverse_vol": ...}, '
    '"rationale": "<one sentence>"}. Every value must come from these lists: '
    + json.dumps({"family": list(FAMILIES), "universe": list(UNIVERSES),
                  "params_by_family": {f: {k: _shown(v) for k, v in CHOICES[f].items()} for f in FAMILIES},
                  "per_order_pct": _shown(SIZING["per_order_pct"]), "inverse_vol": _shown(SIZING["inverse_vol"])})
    + " A reversal recipe's universe cannot be crypto. Prefer ideas unlike the living agents."
)


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _strip_fences(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    return body.strip()


class Scout:
    def __init__(self, client: Any, budget: int) -> None:
        self.client = client
        self.budget = budget
        if hasattr(client, "timeout"):
            client.timeout = TIMEOUT

    def propose(self, memory: dict[str, Any], *, today: date, living: list[dict[str, Any]],
                trials: int) -> tuple[Optional[Recipe], str]:
        week = week_key(today)
        if memory.get("week") != week:
            memory["week"], memory["spent"] = week, 0
        if int(memory.get("spent") or 0) >= self.budget:
            return None, f"the scout's {self.budget} proposals for {week} are spent"
        memory["spent"] = int(memory.get("spent") or 0) + 1
        user = json.dumps({"living_agents": living, "recipes_tried_so_far": trials})
        try:
            text = self.client.complete(SYSTEM, user)
        except Exception as exc:  # noqa: BLE001 — any failure of an optional advisor is a note
            return None, f"the scout could not be reached ({type(exc).__name__})"
        try:
            raw = json.loads(_strip_fences(str(text)))
            recipe = validate(raw["recipe"], origin="scout", parents=())
        except (ValueError, KeyError, TypeError) as exc:
            return None, f"the scout's proposal was refused: {str(exc)[:120]}"
        rationale = " ".join(str(raw.get("rationale") or "").split())[:240]
        memory["last_rationale"] = rationale
        return recipe, rationale
