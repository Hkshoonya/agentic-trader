"""One daily step of the swarm: age, cull, keep the book, breed.

The step only ever reads finished days (P2). It replays every day it missed,
in order, as a daily run would have: cull on what that day knew, with that
day's weekday, then blend and trade at that day's close. A machine that was
off loses no cull and no sample. Births are not replayed: they happen once, at
the newest finished day. A lock stops two timers stepping at once.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from agentic_trading.desk.book import MemberBook
from agentic_trading.fast.service import FastJournal
from agentic_trading.fast.store import FastStore
from agentic_trading.history import Bar
from agentic_trading.swarm.blend import blend_weights, contributing, cull, shares
from agentic_trading.swarm.book import advance, closes_on
from agentic_trading.swarm.breed import crossover, immigrant, mutate
from agentic_trading.swarm.life import Agent, forward_record, through
from agentic_trading.swarm.recipe import Recipe
from agentic_trading.swarm.scout import Scout
from agentic_trading.swarm.screen import duplicate_of, screen
from agentic_trading.swarm.settings import SwarmConfig
from agentic_trading.swarm.store import SwarmState, SwarmStore

STALE_DAYS = 4
RECENT = 20
SIGNATURE_KEEP = 250
MAX_TRIES = 50
BENCH = ("QQQ", "BTCUSD")
NEWS = ("swarm_birth", "swarm_death", "swarm_scout", "swarm_agent_error", "swarm_stale_bars")


@dataclass(frozen=True)
class StepResult:
    code: int
    message: str


def finished_day(series: dict[str, list[Bar]], today: date) -> Optional[date]:
    bars = series.get("BTCUSD") or []
    # P2: a day is finished only once a later BTC bar exists (sync merges the forming candle)
    return min(bars[-1].start.date() - timedelta(days=1), today - timedelta(days=1)) if bars else None


def stale_note(series: dict[str, list[Bar]], today: date) -> str:
    for symbol in BENCH:
        bars = series.get(symbol)
        if not bars:
            return f"there are no {symbol} bars"
        age = (today - bars[-1].start.date()).days
        if age > STALE_DAYS:
            return f"the newest {symbol} bar is {age} days old"
    return ""


def run_step(config: Any, swarm: SwarmConfig, *, now: Optional[datetime] = None,
             series: Optional[dict[str, list[Bar]]] = None, costs: Any = None, cash: Any = None,
             scout_client: Any = None) -> StepResult:
    now = now or datetime.now(timezone.utc)
    store = SwarmStore(config.state_dir)
    if not store.lock(now):
        return StepResult(0, "another swarm step is running; this one stands down")
    try:
        return _Step(config, swarm, store, now, scout_client).run(series, costs, cash)
    finally:
        store.unlock()


def _name(recipe_id: str, lineage: dict[str, dict[str, Any]]) -> str:
    family = ((lineage.get(recipe_id) or {}).get("recipe") or {}).get("family") or "agent"
    return f"{family}-{recipe_id[:4]}"


class _Step:
    def __init__(self, config: Any, swarm: SwarmConfig, store: SwarmStore, now: datetime, scout_client: Any) -> None:
        self.config, self.swarm, self.store, self.now = config, swarm, store, now
        self.scout_client = scout_client
        self.journal = FastJournal(config.journal_dir, prefix="swarm")
        self.events: list[dict[str, Any]] = []

    def say(self, event: str, text: str, **extra: Any) -> None:
        record = {"event": event, "at": self.now.isoformat(), "text": text, **extra}
        self.journal.append(record)
        self.events.append(record)

    def run(self, series: Optional[dict[str, list[Bar]]], costs: Any, cash: Any) -> StepResult:
        from agentic_trading.evidence import load_series
        from agentic_trading.execution import cost_model_for

        series = series if series is not None else load_series(self.config)
        # One convention for the whole step: the whitelist keys crypto as BTC-USD, the swarm as BTCUSD.
        series = {symbol.replace("-", "").upper(): bars for symbol, bars in series.items()}
        self.costs = costs if costs is not None else cost_model_for(self.config.state_dir)
        self.cash = cash if cash is not None else FastStore(self.config.state_dir).starting_equity()
        today = self.now.date()
        note = stale_note(series, today)
        as_of = finished_day(series, today)
        if note or as_of is None:
            text = f"the swarm waits: {note or 'there are no BTCUSD bars'}"
            self.say("swarm_stale_bars", text)
            self.store.write_status({**self.store.read_status(), "stale": True, "note": text,
                                     "checked_at": self.now.isoformat()})
            return StepResult(0, text)
        state, notes = self.store.load(self.now)
        for text in notes:
            self.say("swarm_store_reset", text)
        if state.last_step and state.last_step >= as_of.isoformat():
            return StepResult(0, f"already stepped through {as_of}; nothing to do")
        self.series = through(series, as_of)
        self._records(state, as_of)
        deaths, agent_shares = self._days(state, as_of)
        state.last_step = as_of.isoformat()
        self.store.save(state)  # the book has taken these days: a crash while breeding must not replay them
        births, screens = self._breed(state, as_of)
        for agent in state.living:
            agent.signature.update(zip(agent.record.days, agent.record.returns))
            for key in sorted(agent.signature)[:-SIGNATURE_KEEP]:
                del agent.signature[key]
        state.last_step = as_of.isoformat()
        message = (f"swarm step for {as_of}: {len(state.living)} alive, {screens} screened, {births} born, "
                   f"{deaths} died, {state.trials} recipes tried in all")
        self.say("swarm_step", message, alive=len(state.living), trials=state.trials)
        self.store.save(state)
        self.store.write_status(self._status(state, as_of, agent_shares))
        return StepResult(0, message)

    def _records(self, state: SwarmState, as_of: date) -> None:
        for agent in state.living:
            try:
                agent.record = forward_record(agent.recipe, self.series, date.fromisoformat(agent.born), as_of,
                                              costs=self.costs, cash=self.cash)
                agent.errored = ""
            except Exception as exc:  # noqa: BLE001 — one broken agent must not stop the swarm
                if not agent.errored:
                    self.say("swarm_agent_error", f"{agent.recipe.name} hit an error and sits out "
                                                  f"({type(exc).__name__})", agent=agent.recipe.id)
                agent.errored = f"{type(exc).__name__}: {str(exc)[:120]}"

    def _days(self, state: SwarmState, as_of: date) -> tuple[int, dict[str, float]]:
        """Every day since the last step, in order: cull on what the day knew, then trade at its close."""
        book, reset = MemberBook.load(self.store.book_path, name="swarm", starting_equity=self.cash)
        if reset:
            self.say("swarm_store_reset", "the swarm's book was unreadable; it starts fresh")
        day = date.fromisoformat(state.last_step) + timedelta(days=1) if state.last_step else as_of
        deaths, agent_shares = 0, {}
        taken = str(book.to_dict().get("mark_day") or "")
        while day <= as_of:
            if taken and day.isoformat() <= taken:  # the book already took this day
                day += timedelta(days=1)
                continue
            known = (day + timedelta(days=1)).isoformat()  # records through this day's close
            seen = [Agent(a.recipe, a.born, a.signature, a.errored, a.record.upto(known)) for a in state.living]
            doomed = cull(seen, today=day, max_drawdown_pct=float(self.swarm.max_drawdown) * 100,
                          cull_after_days=self.swarm.cull_after_days)
            for agent, reason in doomed:
                row = state.lineage.setdefault(agent.recipe.id, {"recipe": agent.recipe.to_dict(), "born": agent.born})
                row.update(died=day.isoformat(), cause=reason, excess_pct=agent.record.excess_pct,
                           days=len(agent.record.days))
                self.say("swarm_death", f"{agent.recipe.name} died: {reason}", agent=agent.recipe.id)
            gone = {agent.recipe.id for agent, _ in doomed}
            state.living = [a for a in state.living if a.recipe.id not in gone]
            deaths += len(doomed)
            before = [Agent(a.recipe, a.born, a.signature, a.errored, a.record.upto(day.isoformat()))
                      for a in state.living]  # shares use only days before this one
            agent_shares = shares(before, nursery_days=self.swarm.nursery_days)
            weights = blend_weights(before, agent_shares, self.series, day)
            advance(book, day, weights, closes_on(self.series, day), self.costs)
            day += timedelta(days=1)
        book.save()
        self.book = book
        return deaths, agent_shares

    def _scout(self) -> Optional[Scout]:
        if not self.swarm.llm_scout:
            return None
        client = self.scout_client
        if client is None:
            from agentic_trading.llm.client import FakeLlmClient, build_llm_client
            client = build_llm_client()
            if isinstance(client, FakeLlmClient):
                self.say("swarm_scout", "the scout is on, but AGENTIC_LLM_API_KEY is not set")
                return None
        return Scout(client, self.swarm.llm_proposals_per_week)

    def _bred(self, living: list[Agent], rng: random.Random) -> Recipe:
        proven = [a for a in living if contributing(a, nursery_days=self.swarm.nursery_days)]
        if not proven or rng.random() >= 0.5:
            return immigrant(rng)
        if len(proven) >= 2 and rng.random() < 0.5:
            a, b = rng.sample(proven, 2)
            return crossover(a.recipe, b.recipe, rng)
        return mutate(rng.choice(proven).recipe, rng)

    def _breed(self, state: SwarmState, as_of: date) -> tuple[int, int]:
        rng = random.Random(f"{self.swarm.seed}:{as_of.isoformat()}:{state.trials}")
        birth = as_of + timedelta(days=1)
        scout = self._scout()
        if scout is not None and scout.exhausted(state.scout, as_of):
            scout = None  # spent for the week: say nothing until Monday
        known = set(state.lineage) | {a.recipe.id for a in state.living} | set(state.rejected)
        births = screens = tries = 0
        while (len(state.living) < self.swarm.max_agents and screens < self.swarm.screens_per_day
               and tries < MAX_TRIES):
            tries += 1
            rationale = ""
            if scout is not None:
                candidate, rationale = scout.propose(
                    state.scout, today=as_of, trials=state.trials,
                    living=[{"name": a.recipe.name, **a.recipe.content(), "forward_days": len(a.record.days),
                             "excess_pct": a.record.excess_pct} for a in state.living])
                scout = None  # at most one proposal per step
                if candidate is None:
                    self.say("swarm_scout", rationale)
                    continue
            else:
                candidate = self._bred(state.living, rng)
            if candidate.id in known:
                continue
            known.add(candidate.id)
            state.trials += 1
            screens += 1
            try:
                result = screen(candidate, self.series, birth, costs=self.costs, cash=self.cash)
            except Exception as exc:  # noqa: BLE001 — one bad candidate must not stop the swarm
                state.rejected.append(candidate.id)
                self.say("swarm_rejected", f"{candidate.name} was not born: its screen errored "
                                           f"({type(exc).__name__})", agent=candidate.id)
                continue
            reason = result.reason
            if result.passed:
                twin = duplicate_of(result.signature, state.living)
                if twin:
                    reason = f"a near-copy of {twin}"
            if reason != "passed":
                state.rejected.append(candidate.id)  # never screened (or counted) again
                self.say("swarm_rejected", f"{candidate.name} was not born: {reason}", agent=candidate.id)
                continue
            state.living.append(Agent(candidate, birth.isoformat(), dict(result.signature)))
            state.lineage[candidate.id] = {
                "recipe": candidate.to_dict(), "born": birth.isoformat(), "parents": list(candidate.parents),
                "origin": candidate.origin, "rationale": rationale, "died": None, "cause": "",
                "screen": {"return_pct": result.return_pct, "drawdown_pct": result.drawdown_pct,
                           "trades": result.trades}}
            births += 1
            self.say("swarm_birth", self._birth_text(candidate, state, rationale), agent=candidate.id)
        return births, screens

    def _birth_text(self, recipe: Recipe, state: SwarmState, rationale: str) -> str:
        if recipe.origin == "mutation":
            return f"{recipe.name} was born from {_name(recipe.parents[0], state.lineage)}"
        if recipe.origin == "crossover":
            first, second = (_name(p, state.lineage) for p in recipe.parents)
            return f"{recipe.name} was born from {first} × {second}"
        if recipe.origin == "scout":
            return f"{recipe.name} was born from the scout's idea: {rationale}"
        return f"{recipe.name} was born: a newcomer ({recipe.family}, {recipe.universe})"

    def _status(self, state: SwarmState, as_of: date, agent_shares: dict[str, float]) -> dict[str, Any]:
        agents = []
        for a in state.living:
            days = len(a.record.days)
            if a.errored:
                condition = "errored"
            elif a.recipe.id in agent_shares:
                condition = "contributing"
            elif days < self.swarm.nursery_days:
                condition = "nursery"
            else:
                condition = "waiting"
            agents.append({"id": a.recipe.id, "name": a.recipe.name, "family": a.recipe.family,
                           "universe": a.recipe.universe, "origin": a.recipe.origin, "born": a.born,
                           "forward_days": days, "excess_pct": a.record.excess_pct,
                           "return_pct": a.record.return_pct, "drawdown_pct": a.record.drawdown_pct,
                           "state": condition, "share": round(agent_shares.get(a.recipe.id, 0.0), 4),
                           "errored": a.errored})
        news = [{"at": e["at"], "event": e["event"], "text": e["text"]} for e in reversed(self.events)
                if e["event"] in NEWS]
        recent = (news + list(self.store.read_status().get("recent") or []))[:RECENT]
        start = self.book.starting_equity
        return {
            "as_of": as_of.isoformat(), "stepped_at": self.now.isoformat(), "stale": False, "note": "",
            "trials": state.trials, "alive": len(state.living),
            "book": {"equity": str(round(self.book.equity, 2)),
                     "return_pct": round(float(self.book.equity / start - 1) * 100, 3) if start > 0 else 0.0},
            "agents": agents, "recent": recent,
            "scout": {"enabled": self.swarm.llm_scout, "spent": int(state.scout.get("spent") or 0),
                      "budget": self.swarm.llm_proposals_per_week,
                      "last_rationale": str(state.scout.get("last_rationale") or "")},
        }
