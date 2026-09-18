"""Local animated dashboard: live executions, equity flow, promotion state.

Stdlib only (``http.server``) so it runs wherever the bot runs, with no CDN and
no outbound network access. Every endpoint is read-only: state is derived from
the decision journal, RiskGuard state, and evolution/promotion files. Nothing
here can place an order.

Binds to ``127.0.0.1`` by default — this is an operator console, not a public
web app, and the journal contains account activity.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from agentic_trading.config import Config, load_config
from agentic_trading.dashboard_html import HTML
from agentic_trading.jsonio import dumps as json_dumps
from agentic_trading.promotion import load_state
from agentic_trading.runtime import effective_mode
from agentic_trading.session import next_session_open, session_allows, session_for


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_stamp(value: str) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _positive(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (ArithmeticError, TypeError, ValueError):
        return False


def _grade_from_journal(
    record: dict[str, Any],
    *,
    symbol: Optional[str],
    at: str = "",
    advisor: dict[str, Any],
    regime: dict[str, list[dict[str, Any]]],
    features_as_of: Any = None,
) -> Optional[dict[str, Any]]:
    """Grade an order from the snapshot the journal recorded at decision time.

    Older records predate per-order grading. The features they were decided on
    are still in the regime record's ``market`` block, so the grade can be
    rebuilt from observed data rather than guessed. The snapshot is the last one
    taken *at or before* the decision: grading with a reading from eleven hours
    later would describe a different market and quietly mis-explain the trade.
    The payload is labelled ``journal`` and carries the snapshot time, so what
    it was built from can be checked.

    Where the journal never recorded a snapshot at all, the features are rebuilt
    from the stored bars cut off at the decision time and labelled ``bars_asof``
    — the reading is the same series the strategy traded on, but it is a rebuild,
    and calling it anything else would be dishonest.
    """
    if not symbol:
        return None
    entries = regime.get(str(symbol).upper()) or []
    when = _parse_stamp(at)
    at_or_before = [entry for entry in entries if not at or str(entry.get("at") or "") <= at]
    view = at_or_before[-1] if at_or_before else (entries[0] if entries else None)
    market = view.get("market") if isinstance(view, dict) else None
    rebuilt = False
    features: Any = None
    if isinstance(market, dict) and market:
        from agentic_trading.llm.market import MarketFeatures

        try:
            features = MarketFeatures(
                symbol=str(symbol),
                bars=int(market.get("bars") or 0),
                last_close=float(market.get("last_close") or 0.0),
                ret_1_pct=float(market.get("ret_1_pct") or 0.0),
                ret_5_pct=float(market.get("ret_5_pct") or 0.0),
                ret_20_pct=float(market.get("ret_20_pct") or 0.0),
                vol_pct=float(market.get("vol_pct") or 0.0),
                trend_pct=float(market.get("trend_pct") or 0.0),
                from_high_pct=float(market.get("from_high_pct") or 0.0),
                range_position=float(market.get("range_position") or 0.0),
                spread_bps=(
                    None
                    if market.get("spread_bps") is None
                    else float(market["spread_bps"])
                ),
                volume_z=(
                    None if market.get("volume_z") is None else float(market["volume_z"])
                ),
                volume_coverage=float(market.get("volume_coverage") or 0.0),
            )
        except (TypeError, ValueError):
            features = None
    if features is None and features_as_of is not None and when is not None:
        try:
            features = features_as_of(str(symbol), when)
        except Exception:  # noqa: BLE001 — an unavailable rebuild is not an error
            features = None
        rebuilt = features is not None
    if features is None:
        return None
    try:
        from agentic_trading.confidence import grade_order

        grade = grade_order(
            features,
            regime=view,
            advisor=advisor or None,
            blocked_by=(
                str(record.get("reason", "")) if record.get("event") == "rejected" else None
            ),
        )
    except (TypeError, ValueError):
        return None
    payload = grade.to_dict()
    payload["source"] = "bars_asof" if rebuilt else "journal"
    payload["snapshot_at"] = (
        at if rebuilt else str((view or {}).get("at") or "")
    )
    payload["snapshot_exact"] = bool(at) and str((view or {}).get("at") or "") <= at
    if rebuilt:
        payload["basis"] = (
            "rebuilt from stored bars cut off at the decision time: the journal "
            "recorded no market snapshot for this order"
        )
    return payload


def _evidence_view(
    report: Optional[dict[str, Any]],
    *,
    auto_refresh_days: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Trim the walk-forward report to what the console shows.

    Kept deliberately short: the console answers "is the size I am trading still
    justified?", and the two numbers that do that are the drawdown of the size
    being traded and the largest size the ceiling allows.
    """
    if not report:
        return None
    configs = report.get("configs") if isinstance(report.get("configs"), dict) else {}

    def pick(entry: Any) -> Optional[dict[str, Any]]:
        if not isinstance(entry, dict):
            return None
        return {
            "per_order_pct": entry.get("per_order_pct"),
            "trades": entry.get("trades"),
            "expectancy_bps": entry.get("expectancy_bps"),
            "max_drawdown_pct": entry.get("max_drawdown_pct"),
            "final_equity": entry.get("final_equity"),
            "bootstrap_p_value": entry.get("bootstrap_p_value"),
            "profit_factor": entry.get("profit_factor"),
            "eligible": entry.get("eligible"),
        }

    return {
        "generated_at": report.get("generated_at", ""),
        "auto_refresh_days": auto_refresh_days,
        "refreshed_by_daemon": bool(report.get("age_at_build_days")),
        "drawdown_ceiling_pct": report.get("drawdown_ceiling_pct"),
        "symbols": (report.get("series") or {}).get("symbols", []),
        "bars": (report.get("series") or {}).get("bars"),
        "folds": report.get("folds"),
        "notes": report.get("notes") or [],
        "production": pick(configs.get("production")),
        "inverse_vol": pick(configs.get("inverse_vol")),
        "gate_size": pick(report.get("gate_size")),
        "gate_reason": (report.get("gate_size") or {}).get("reason"),
    }


class DashboardState:
    """Reads bot state from disk. Every method is read-only."""

    def __init__(self, config: Config, *, config_path: Path | str | None = None) -> None:
        self.config = config
        self._config_path = Path(config_path) if config_path else None
        self._config_stamp: Optional[tuple[int, int]] = self._stat_config()
        self._config_lock = threading.Lock()
        self.journal_dir = Path(config.journal_dir)
        self.state_dir = Path(config.state_dir)
        # Bar files are large and change at most once a day, so the as-of
        # feature rebuild for the order table keeps them for a while.
        self._bars_cache: dict[str, tuple[float, list[Any]]] = {}
        self._bars_lock = threading.Lock()

    def _bars_for(self, symbol: str) -> list[Any]:
        import time as _time

        now = _time.monotonic()
        with self._bars_lock:
            cached = self._bars_cache.get(symbol)
            if cached is not None and (now - cached[0]) < 600:
                return cached[1]
        from agentic_trading.history import load_bars
        from agentic_trading.history_sync import bar_stem

        directory = Path(self.config.history_path or "data/bars")
        path = directory / f"{bar_stem(symbol)}_day.jsonl"
        bars: list[Any] = []
        if path.is_file():
            try:
                bars = load_bars(path)
            except Exception:  # noqa: BLE001 — an unreadable file is not a crash
                bars = []
        with self._bars_lock:
            self._bars_cache[symbol] = (now, bars)
        return bars

    def features_as_of(self, symbol: str, when: datetime) -> Any:
        """Market features from stored bars, cut off at ``when``.

        Used only where the journal never recorded what the model saw. Stored
        bars are the same series the runtime reads, so on the 82 instants where
        both exist the rebuilt grade lands within 0.04 of the journaled one —
        close enough to explain a decision, which is why it is labelled as a
        rebuild rather than presented as the original reading.
        """
        from agentic_trading.llm.market import features_from_closes

        closes = [float(bar.close) for bar in self._bars_for(symbol) if bar.start <= when]
        if len(closes) < 30:
            return None
        return features_from_closes(symbol.upper(), closes[-260:])

    def _stat_config(self) -> Optional[tuple[int, int]]:
        if self._config_path is None:
            return None
        try:
            info = self._config_path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def refresh_config(self) -> Config:
        """Re-read the config when the operator edits it under a live console.

        The console runs for days while ``config/agentic.toml`` changes (new
        strategy, whitelist, session policy, caps). Serving the boot-time copy
        would quietly misreport what the bot is doing — the exact failure that
        showed a crypto daemon as ``strategy = "fixture"`` with ``["SPY"]``.
        """
        if self._config_path is None:
            return self.config
        with self._config_lock:
            stamp = self._stat_config()
            if stamp == self._config_stamp:
                return self.config
            try:
                config = load_config(self._config_path)
            except Exception:  # noqa: BLE001 — a half-written TOML must not 500
                return self.config
            self.config = config
            self._config_stamp = stamp
            self.journal_dir = Path(config.journal_dir)
            self.state_dir = Path(config.state_dir)
            return config

    def journal_path(self) -> Path:
        return self.journal_dir / f"{date.today().isoformat()}.jsonl"

    def read_records(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        path = self.journal_path()
        if not path.is_file():
            return []
        return self._read_file(path, limit=limit)

    def recent_records(self, *, days: int = 3, limit: int = 4000) -> list[dict[str, Any]]:
        """The last few journals, oldest first.

        The order table is decision-driven and this strategy decides once a day,
        so a today-only table is empty every morning until the rebalance fires —
        which reads as a broken console. Counters still come from today's file;
        only the table looks back far enough to have something to say.
        """
        try:
            files = sorted(
                (
                    path
                    for path in self.journal_dir.glob("*.jsonl")
                    if path.name[:1].isdigit()
                ),
                key=lambda path: path.name,
            )[-days:]
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for path in files:
            # Journal files are named by *local* date while every timestamp
            # inside is UTC, so the only reliable way to know which day a
            # decision belongs to is to remember which file it came from.
            # Comparing the UTC date to the local date silently counted last
            # night's decisions as today's.
            records.extend(
                {**record, "_journal_day": path.stem}
                for record in self._read_file(path, limit=limit)
            )
        return records[-limit:]

    @staticmethod
    def _read_file(path: Path, *, limit: int = 2000) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return records[-limit:]

    def cadence(self) -> dict[str, Any]:
        """When this strategy actually decides, in plain language.

        The console refreshes every two seconds, which makes a daily-rebalance
        strategy look frozen: the stream moves, the order table does not, and
        there is nothing on screen that explains why. This is that explanation,
        computed from the strategy's own state file rather than guessed.
        """
        now = datetime.now(timezone.utc)
        state_file = self.state_dir / f"strategy_{self.config.strategy}.json"
        state = _read_json(state_file) or {}
        last_date = str(state.get("last_decision_date") or "")
        quantities = state.get("quantities") if isinstance(state, dict) else {}
        held = [
            symbol
            for symbol, qty in (quantities or {}).items()
            if _positive(qty)
        ]
        # The strategy rebalances on the first quote of each UTC day.
        next_at = datetime.combine(
            now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
        if last_date < now.date().isoformat():
            next_at = now  # the next quote of this session triggers it
        last_at = ""
        for record in reversed(self.read_records(limit=4000)):
            intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
            if record.get("event") in ("accepted", "rejected", "placed", "place_failed"):
                last_at = str(intent.get("created_at") or record.get("at") or "")
                break
        return {
            "strategy": self.config.strategy,
            "rebalance": "daily_utc",
            "schedule": "once per UTC day (first quote after 00:00 UTC)",
            "last_decision_date": last_date,
            "last_decision_at": last_at,
            "rebalanced_today": last_date == now.date().isoformat(),
            "next_decision_at": next_at.isoformat(),
            "next_decision_local": next_at.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            "held": held,
            "explanation": (
                f"{self.config.strategy} rebalances once per UTC day, on the "
                "first quote after 00:00 UTC; between rebalances the order "
                "table is static by design while the cycle stream keeps running."
            ),
        }

    def candidates(self) -> dict[str, Any]:
        """What the rule wants right now, and what is stopping each name.

        Read-only and cached: it loads the same bar files the strategy reads, so
        the answer is the strategy's own arithmetic rather than a second
        opinion, but it costs a file read per symbol and the console polls.
        """
        now = datetime.now(timezone.utc)
        cache = getattr(self, "_candidates_cache", None)
        if cache is not None and (now - cache[0]).total_seconds() < 60:
            return cache[1]
        from agentic_trading.evidence import load_series
        from agentic_trading.walkforward import rank_targets

        try:
            series = load_series(self.config)
            rows = rank_targets(
                series,
                now,
                max_positions=self.config.max_open_positions,
            )
        except Exception as exc:  # noqa: BLE001 — the console must still render
            payload: dict[str, Any] = {"rows": [], "error": str(exc)[:200]}
            self._candidates_cache = (now, payload)
            return payload

        regime = self._latest_regimes()
        cadence = self.cadence()
        held = {name.upper() for name in cadence["held"]}
        for row in rows:
            symbol = str(row["symbol"]).upper()
            broker_form = symbol if "-" in symbol else symbol
            row["held"] = symbol.replace("-", "") in held or broker_form in held
            view = regime.get(broker_form) or regime.get(symbol)
            row["regime"] = None if view is None else view.get("regime")
            row["regime_confidence"] = (
                None if view is None else view.get("confidence")
            )
            blockers: list[str] = []
            # Mirror the gate exactly: a chop/panic read only blocks when the
            # model is at least DEFAULT_BLOCK_CONFIDENCE sure. Quoting the view's
            # raw flag here would claim SPY is blocked at c=0.55 when the gate
            # would let it through, which is worse than saying nothing.
            from agentic_trading.llm.regime import DEFAULT_BLOCK_CONFIDENCE

            if (
                row["selected"]
                and view is not None
                and view.get("blocks_entries")
                and float(view.get("confidence") or 0.0) >= DEFAULT_BLOCK_CONFIDENCE
            ):
                blockers.append(
                    f"regime {view.get('regime')} c={float(view.get('confidence') or 0):.2f}"
                )
            if row["selected"] and row["held"]:
                blockers.append("already held")
            row["blocked_by"] = blockers
        payload = {
            "rows": rows,
            "generated_at": now.isoformat(),
            "selected": [row["symbol"] for row in rows if row["selected"]],
            "blocked": [
                row["symbol"] for row in rows if row["selected"] and row["blocked_by"]
            ],
            "note": (
                "The rule re-reads the tape every cycle but only trades at the "
                "daily rebalance; this is what it would hold if it decided now."
            ),
            "cadence": cadence,
        }
        self._candidates_cache = (now, payload)
        return payload

    def _candidate_summary(self) -> dict[str, Any]:
        """The cached candidate headline, for the two-second summary poll.

        ``candidates()`` costs a bar-file read per symbol, so the summary only
        reports what the last computation found; the panel itself refreshes on
        its own timer.
        """
        cache = getattr(self, "_candidates_cache", None)
        if cache is None:
            return {}
        payload = cache[1]
        return {
            "selected": payload.get("selected") or [],
            "blocked": payload.get("blocked") or [],
            "generated_at": payload.get("generated_at", ""),
        }

    def _size_floor(self, limits: dict[str, Any], risk: dict[str, Any]) -> dict[str, Any]:
        """Whether the account is big enough to trade the size the evidence allows.

        On 2026-09-18 the agent sat in live mode refusing every entry with
        `below_min_notional`: at $50 and a 0.92% ceiling the order is $0.46, and
        the broker minimum is $1.00. That is the guard working — it refuses
        rather than oversizing — but five identical rejects are a poor way to
        learn "your account is too small". Say it once, with the number that
        would fix it.
        """
        from decimal import Decimal

        try:
            cap = Decimal(str(limits.get("max_order_pct") or self.config.max_order_pct))
        except (ArithmeticError, TypeError, ValueError):
            return {}
        try:
            equity = Decimal(str(risk.get("current_equity") or "0"))
        except (ArithmeticError, TypeError, ValueError):
            equity = Decimal("0")
        minimum = Decimal(str(self.config.min_order_notional))
        if cap <= 0:
            return {}
        needed = (minimum / cap).quantize(Decimal("0.01"))
        order = (equity * cap).quantize(Decimal("0.01"))
        ceiling = Decimal(str(getattr(self.config, "small_account_max_order_pct", "0") or "0"))
        floor_active = bool(ceiling > 0 and equity > 0 and equity < needed)
        effective = cap
        if floor_active:
            effective = min(
                (minimum / equity) * Decimal("1.02"), ceiling
            )
        return {
            "min_order_notional": str(minimum),
            "order_at_ceiling": str(order),
            "equity_needed": str(needed),
            "too_small_to_trade": bool(equity > 0 and equity < needed),
            "small_account_max_order_pct": str(ceiling),
            "size_floor_active": floor_active,
            "effective_order_pct": str(effective),
            "effective_order_notional": str(
                (equity * effective).quantize(Decimal("0.01"))
            ),
            "drawdown_at_effective_pct": self._drawdown_at(effective),
        }

    def _drawdown_at(self, per_order_pct: Any) -> Optional[float]:
        """What the walk-forward measured at (or nearest to) this size.

        Sizing up on a small account is a trade: more drawdown for the chance to
        see results. The number comes from the evidence report's own frontier, so
        the console can state the cost instead of implying there is none.
        """
        report = _read_json(self.state_dir / "strategy_evidence.json") or {}
        frontier = report.get("size_frontier") or []
        try:
            wanted = float(per_order_pct)
        except (TypeError, ValueError):
            return None
        rows = [
            row
            for row in frontier
            if isinstance(row, dict) and row.get("per_order_pct") is not None
        ]
        if not rows or wanted <= 0:
            return None
        nearest = min(rows, key=lambda row: abs(float(row["per_order_pct"]) - wanted))
        # Only quote it when it is genuinely close to the size being traded.
        if abs(float(nearest["per_order_pct"]) - wanted) > max(0.005, wanted * 0.5):
            return None
        value = nearest.get("max_drawdown_pct")
        return None if value is None else float(value)

    def decision_counts(self, *, days: int = 3) -> dict[str, int]:
        """Decision counters for the *strategy's* day, which is UTC.

        The strategy rebalances once per UTC day, so its decision for today is
        taken at 00:00 UTC — 20:00 the previous evening in New York. Journals are
        named by local date, which put this morning's whole trading day in
        yesterday's file and left the counters reading 0/0/0 all day while the
        table showed rows. Counting both by UTC removes the contradiction.
        """
        today_utc = datetime.now(timezone.utc).date().isoformat()
        counts = {"accepted": 0, "placed": 0, "rejected": 0, "failed": 0}
        for record in self.recent_records(days=days):
            event = str(record.get("event") or "")
            if event not in ("accepted", "placed", "rejected", "place_failed"):
                continue
            at = str(
                (record.get("intent") or {}).get("created_at")
                or record.get("at")
                or ""
            )
            if at[:10] != today_utc:
                continue
            key = "failed" if event == "place_failed" else event
            counts[key] += 1
        return counts

    def pulse(self) -> dict[str, Any]:
        """How long since the agent last did anything.

        A stopped daemon leaves every panel frozen at plausible values, which is
        indistinguishable from a quiet market — the exact question "why did it
        stop?" that took a manual log dig to answer on 2026-09-18.
        """
        now = datetime.now(timezone.utc)
        records = self.read_records()
        newest = ""
        for record in reversed(records):
            newest = str(record.get("at") or "")
            if newest:
                break
        age: Optional[float] = None
        seen = _parse_stamp(newest)
        if seen is not None:
            age = (now - seen).total_seconds()
        stale_after = 300.0
        return {
            "last_event_at": newest,
            "silent_seconds": age,
            "stale_after_seconds": stale_after,
            "silent": age is not None and age > stale_after,
            "events_today": len(records),
        }

    def _latest_regimes(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in self.read_records(limit=4000):
            if record.get("event") != "regime":
                continue
            symbol = str(record.get("symbol") or "").upper()
            if symbol:
                latest[symbol] = record
        return latest

    def summary(self) -> dict[str, Any]:
        self.refresh_config()
        records = self.read_records()
        risk = _read_json(self.state_dir / "risk_guard.json") or {}
        gate = _read_json(self.state_dir / "live_gate.json") or {}
        limits = _read_json(self.state_dir / "effective_limits.json") or {}
        agents = _read_json(self.state_dir / "agents.json") or {}
        health = _read_json(self.state_dir / "health.json") or {}
        promotion = load_state(self.state_dir)
        evolution = _read_json(self.state_dir / "evolution.json")
        now = datetime.now(timezone.utc)
        session = session_for(now)

        counts: dict[str, int] = {}
        for record in records:
            event = str(record.get("event", "unknown"))
            counts[event] = counts.get(event, 0) + 1

        # Latest regime classification per symbol, as the LLM gate sees it.
        regimes: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get("event") != "regime":
                continue
            symbol = str(record.get("symbol") or "")
            if symbol:
                regimes[symbol] = {
                    "regime": record.get("regime", ""),
                    "confidence": record.get("confidence"),
                    "reason": record.get("reason", ""),
                    "blocks_entries": record.get("blocks_entries"),
                    "at": record.get("at", ""),
                    "model": record.get("model", ""),
                }

        # The agent may widen trading hours within the operator's bound, so the
        # console reports the policy actually in force, not the configured one.
        effective_policy = str(limits.get("session_policy") or "") or (
            self.config.session_policy
        )
        details = limits.get("details") if isinstance(limits.get("details"), dict) else {}
        return {
            "mode": effective_mode(self.config),
            "kill_switch": bool(risk.get("kill_switch", False)),
            "kill_reason": risk.get("kill_reason", ""),
            "daily_notional": risk.get("daily_notional", "0"),
            "baseline_equity": risk.get("baseline_equity", "0"),
            "current_equity": risk.get("current_equity", "0"),
            "session": session,
            "session_allowed": session_allows(effective_policy, session),
            "session_policy": effective_policy,
            "configured_session_policy": self.config.session_policy,
            "next_open": next_session_open(now).isoformat(),
            "strategy": self.config.strategy,
            "symbols": sorted(self.config.symbol_whitelist),
            "quote_source": self.config.quote_source,
            "autonomy": self.config.autonomy,
            # Whether the daemon is actually armed to submit real orders (it
            # publishes its own environment; the console cannot read it).
            "armed": bool(gate.get("allow_live", False)),
            "autonomy_enabled": bool(gate.get("allow_autonomy", False)),
            "gate_updated_at": gate.get("updated_at", ""),
            "event_counts": {**counts, **self.decision_counts()},
            "regimes": regimes,
            "agents": agents.get("agents", []),
            "agents_updated_at": agents.get("updated_at", ""),
            "alerts": [
                {
                    "at": record.get("at", ""),
                    "title": record.get("title", ""),
                    "body": record.get("body", ""),
                    "key": record.get("key", ""),
                    "channels": record.get("channels", []),
                    "urgency": record.get("urgency", ""),
                }
                for record in records[-400:]
                if record.get("event") == "notify"
            ][-8:],
            "health": {
                "healthy": health.get("healthy"),
                "checked_at": health.get("finished_at", ""),
                "ok": health.get("ok", 0),
                "warnings": [
                    {"name": item.get("name"), "detail": item.get("detail")}
                    for item in (health.get("warnings") or [])
                ],
                "failures": [
                    {"name": item.get("name"), "detail": item.get("detail")}
                    for item in (health.get("failures") or [])
                ],
                "checks": health.get("checks") or [],
            },
            "risk": {
                "max_order_pct": limits.get("max_order_pct", str(self.config.max_order_pct)),
                **self._size_floor(limits, risk),
                "daily_notional_pct": limits.get(
                    "daily_notional_pct", str(self.config.daily_notional_pct)
                ),
                "confidence": limits.get("confidence", "0"),
                "reason": limits.get("reason", ""),
                "updated_at": limits.get("updated_at", ""),
                "target_max_order_pct": details.get("target_max_order_pct"),
                "ceiling_max_order_pct": str(self.config.max_order_pct),
                "ceiling_daily_notional_pct": str(self.config.daily_notional_pct),
                "components": details.get("components") or {},
            },
            "promotion": {
                "stage": promotion.stage,
                "streak": promotion.streak,
                "required_cycles": self.config.promotion_cycles_required,
                "last_assessment": promotion.last_assessment,
                "updated_at": promotion.updated_at,
            },
            "evolution": evolution,
            # The walk-forward evidence the order size is justified by. Written
            # by `agentic-trading walkforward`; absent until that has run once.
            # Why the order table is allowed to be static, and what the rule
            # would hold if it decided this second.
            "cadence": self.cadence(),
            "pulse": self.pulse(),
            "candidate_summary": self._candidate_summary(),
            "evidence": _evidence_view(
                _read_json(self.state_dir / "strategy_evidence.json"),
                auto_refresh_days=self.config.evidence_refresh_days,
            ),
            "generated_at": now.isoformat(),
        }

    def records_since(self, offset: int) -> dict[str, Any]:
        self.refresh_config()
        records = self.read_records()
        offset = max(0, offset)
        return {
            "offset": len(records),
            "records": [_compact(record) for record in records[offset:]],
        }

    def equity_curve(self) -> dict[str, Any]:
        """Order notionals over time, plus current RiskGuard equity state."""
        self.refresh_config()
        records = self.read_records()
        points: list[dict[str, Any]] = []
        cumulative = Decimal("0")
        for record in records:
            if record.get("event") != "accepted":
                continue
            intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
            notional = Decimal(str(record.get("notional", "0") or "0"))
            cumulative += notional
            points.append(
                {
                    "at": intent.get("created_at") or "",
                    "notional": float(notional),
                    "cumulative": float(cumulative),
                    "side": record.get("side"),
                    "symbol": record.get("symbol"),
                    "mode": record.get("mode"),
                }
            )
        risk = _read_json(self.state_dir / "risk_guard.json") or {}
        return {
            "points": points[-500:],
            "baseline_equity": risk.get("baseline_equity", "0"),
            "current_equity": risk.get("current_equity", "0"),
            "daily_notional": risk.get("daily_notional", "0"),
        }

    def orders_table(self, *, limit: int = 60) -> dict[str, Any]:
        """Every order the agent decided on today, newest first."""
        self.refresh_config()
        rows: list[dict[str, Any]] = []
        # Three days, not one: a daily-rebalance strategy would otherwise show an
        # empty table every morning, which is indistinguishable from a broken one.
        records = self.recent_records(days=3)
        today_utc = datetime.now(timezone.utc).date().isoformat()
        # The advisor's opinion is journaled as its own record keyed by
        # decision_id, so join it back in: it is the per-order confidence, as
        # opposed to the system-wide evidence grade.
        advisor_by_decision: dict[str, dict[str, Any]] = {}
        # Every regime read, in time order, so a decision can be graded from the
        # snapshot that was current when it was taken rather than from whatever
        # the model happened to say hours later.
        regime_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            decision_id = record.get("decision_id")
            if decision_id and record.get("event") == "advisor":
                advisor_by_decision[str(decision_id)] = record
            # The regime record carries the market snapshot the model was
            # shown, so a decision made before per-order grading existed can
            # still be graded from what was actually observed at the time.
            if record.get("event") == "regime" and record.get("symbol"):
                symbol = str(record["symbol"]).upper()
                regime_by_symbol.setdefault(symbol, []).append(record)
        for entries in regime_by_symbol.values():
            entries.sort(key=lambda entry: str(entry.get("at") or ""))

        for record in records:
            event = record.get("event")
            if event not in ("accepted", "placed", "rejected", "place_failed"):
                continue
            request = (
                record.get("order_request")
                if isinstance(record.get("order_request"), dict)
                else {}
            )
            intent = (
                record.get("intent") if isinstance(record.get("intent"), dict) else {}
            )
            review = record.get("review") if isinstance(record.get("review"), dict) else {}
            alerts = (
                review.get("data", {}).get("order_checks")
                if isinstance(review.get("data"), dict)
                else review.get("order_checks")
            )
            quote = (
                review.get("data", {}).get("quote_data")
                if isinstance(review.get("data"), dict)
                else None
            )
            confidence = (
                record.get("confidence")
                if isinstance(record.get("confidence"), dict)
                else {}
            )
            advisor = advisor_by_decision.get(str(record.get("decision_id") or ""), {})
            symbol = (
                record.get("symbol")
                or request.get("symbol")
                or intent.get("symbol")
            )
            at = intent.get("created_at") or record.get("at") or ""
            day = str(record.get("_journal_day") or str(at)[:10])
            utc_day = str(at)[:10] or day
            rows.append(
                {
                    "at": at,
                    "day": day,
                    "utc_day": utc_day,
                    "older": bool(utc_day) and utc_day != today_utc,
                    "event": event,
                    "reason": record.get("reason", ""),
                    "symbol": symbol,
                    "side": record.get("side") or request.get("side") or intent.get("side"),
                    "type": request.get("type", ""),
                    "market_hours": request.get("market_hours", ""),
                    "quantity": (
                        request.get("quantity")
                        or record.get("quantity")
                        or intent.get("quantity")
                    ),
                    "dollar_amount": request.get("dollar_amount"),
                    "limit_price": request.get("limit_price"),
                    "notional": record.get("notional", "0"),
                    "mode": record.get("mode", ""),
                    "session": record.get("session", ""),
                    # A rejected order never reaches the broker's quote review,
                    # but the decision itself carries the price it was made at.
                    # Showing "—" there hides the only price the row has.
                    "last_price": (quote or {}).get("last_trade_price")
                    or intent.get("ref_price"),
                    "last_price_source": (
                        "quote" if (quote or {}).get("last_trade_price") else (
                            "decision" if intent.get("ref_price") else ""
                        )
                    ),
                    "alerts": alerts if isinstance(alerts, dict) else {},
                    "ref_id": request.get("ref_id"),
                    "confidence": {
                        "evidence": confidence.get("evidence"),
                        "advisor": confidence.get("advisor", advisor.get("confidence")),
                        "advisor_action": confidence.get(
                            "advisor_action", advisor.get("action")
                        ),
                        "advisor_model": confidence.get(
                            "advisor_model", advisor.get("model")
                        ),
                        "order": confidence.get("order")
                        or _grade_from_journal(
                            record,
                            symbol=symbol,
                            at=at,
                            advisor=advisor,
                            regime=regime_by_symbol,
                            features_as_of=self.features_as_of,
                        ),
                    },
                }
            )
        rows.reverse()
        # Same rule as decision_counts(): the trading day is the UTC day.
        today_rows = [row for row in rows if not row["older"]]
        shown = rows[:limit]
        return {
            "rows": shown,
            # The card above the table says "today", so these must stay scoped to
            # today even though the table itself reaches back far enough to have
            # something in it on a quiet morning.
            "counts": {
                "accepted": sum(1 for r in today_rows if r["event"] == "accepted"),
                "placed": sum(1 for r in today_rows if r["event"] == "placed"),
                "rejected": sum(1 for r in today_rows if r["event"] == "rejected"),
                "failed": sum(1 for r in today_rows if r["event"] == "place_failed"),
            },
            "days_shown": 3,
            # Counted on the slice the table actually renders, so the footer
            # cannot claim more rows than are on screen.
            "older_rows": sum(1 for r in shown if r["older"]),
        }


def _compact(record: dict[str, Any]) -> dict[str, Any]:
    """Trim journal records for the wire, keeping operator-relevant fields."""
    keep = (
        "event",
        "reason",
        "symbol",
        "side",
        "quantity",
        "notional",
        "mode",
        "session",
        "would_place",
        "may_place",
        "decision_id",
        "kind",
        "count",
        "symbols",
        "error",
        "consecutive_errors",
        "order_request",
        "cycles",
        "avg_cycle_seconds",
        "fresh_quotes",
        "decisions",
        "window_seconds",
        "allow_live",
        "allow_autonomy",
        "stage",
        "confidence",
        "title",
        "body",
        "channels",
        "urgency",
        "key",
        "healthy",
        "ok",
        "failures",
        "warnings",
    )
    compact = {key: record[key] for key in keep if key in record}
    intent = record.get("intent")
    if isinstance(intent, dict):
        compact["intent"] = {
            key: intent[key]
            for key in ("symbol", "side", "reason", "created_at", "quantity")
            if key in intent
        }
    compact["at"] = (
        (intent or {}).get("created_at") if isinstance(intent, dict) else None
    ) or record.get("at") or ""
    return compact


class _Handler(BaseHTTPRequestHandler):
    state: DashboardState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(
            status,
            json_dumps(payload).encode("utf-8"),
            "application/json",
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            self._json(self.state.summary())
            return
        if parsed.path == "/api/equity":
            self._json(self.state.equity_curve())
            return
        if parsed.path == "/api/orders":
            self._json(self.state.orders_table())
            return
        if parsed.path == "/api/candidates":
            self._json(self.state.candidates())
            return
        if parsed.path == "/api/journal":
            params = parse_qs(parsed.query)
            try:
                offset = int(params.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            self._json(self.state.records_since(offset))
            return
        if parsed.path == "/api/health":
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, status=404)


def serve(
    config: Config,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
    config_path: Path | str | None = None,
) -> ThreadingHTTPServer:
    """Build the dashboard server (call ``serve_forever()`` to block)."""
    state = DashboardState(config, config_path=config_path)
    handler = type("BoundHandler", (_Handler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    return server
