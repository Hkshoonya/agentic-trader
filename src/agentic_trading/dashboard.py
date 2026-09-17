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
from datetime import date, datetime, timezone
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


def _grade_from_journal(
    record: dict[str, Any],
    *,
    symbol: Optional[str],
    advisor: dict[str, Any],
    regime: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Grade an order from the snapshot the journal recorded at decision time.

    Older records predate per-order grading. The features they were decided on
    are still in the regime record's ``market`` block, so the grade can be
    rebuilt from observed data rather than guessed — and it is labelled
    ``journal`` so it is never confused with a grade the runtime computed.
    """
    if not symbol:
        return None
    view = regime.get(str(symbol).upper())
    market = (view or {}).get("market") if isinstance(view, dict) else None
    if not isinstance(market, dict) or not market:
        return None
    try:
        from agentic_trading.confidence import grade_order
        from agentic_trading.llm.market import MarketFeatures

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
    payload["source"] = "journal"
    return payload


def _evidence_view(report: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
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
            "event_counts": counts,
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
            "evidence": _evidence_view(_read_json(self.state_dir / "strategy_evidence.json")),
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
        records = self.read_records()
        # The advisor's opinion is journaled as its own record keyed by
        # decision_id, so join it back in: it is the per-order confidence, as
        # opposed to the system-wide evidence grade.
        advisor_by_decision: dict[str, dict[str, Any]] = {}
        regime_by_symbol: dict[str, dict[str, Any]] = {}
        for record in records:
            decision_id = record.get("decision_id")
            if decision_id and record.get("event") == "advisor":
                advisor_by_decision[str(decision_id)] = record
            # The regime record carries the market snapshot the model was
            # shown, so a decision made before per-order grading existed can
            # still be graded from what was actually observed at the time.
            if record.get("event") == "regime" and record.get("symbol"):
                regime_by_symbol[str(record["symbol"]).upper()] = record

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
            rows.append(
                {
                    "at": intent.get("created_at") or record.get("at") or "",
                    "event": event,
                    "reason": record.get("reason", ""),
                    "symbol": record.get("symbol") or request.get("symbol") or intent.get("symbol"),
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
                    "last_price": (quote or {}).get("last_trade_price"),
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
                            symbol=(
                                record.get("symbol")
                                or request.get("symbol")
                                or intent.get("symbol")
                            ),
                            advisor=advisor,
                            regime=regime_by_symbol,
                        ),
                    },
                }
            )
        rows.reverse()
        return {
            "rows": rows[:limit],
            "counts": {
                "accepted": sum(1 for r in rows if r["event"] == "accepted"),
                "placed": sum(1 for r in rows if r["event"] == "placed"),
                "rejected": sum(1 for r in rows if r["event"] == "rejected"),
                "failed": sum(1 for r in rows if r["event"] == "place_failed"),
            },
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
