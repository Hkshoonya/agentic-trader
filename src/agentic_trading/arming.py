"""Arming: the one write the console is allowed to make.

Order submission requires a capability in the process environment and an
eligible arm file in this workspace. The capability is deliberately not a
config file, so a copied workspace cannot arm another machine; the arm file is
revalidated on use, so the environment cannot bypass evidence by itself.

A fresh install has neither, so it starts inert.

The rules, in one place:

1. Arming is an explicit human act: a POST with `{"confirm": "ARM"}`.
2. It is refused unless every pre-flight check passes — current eligible
   evidence and stage, clear kill switch, readable account, budget inside the
   operator ceiling, and a healthy back-check from the running daemon.
3. It is refused unless the request came from loopback, because the console is
   bound to loopback but that is not the same as checking.
4. Every change is journalled and timestamped, and disarming is always allowed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ARM_FILE = "arm.json"
CONFIRM_PHRASE = "ARM"

# How stale the evidence may be and still justify arming.
MAX_EVIDENCE_AGE_DAYS = 30.0
# The dashboard is a separate process from the daemon. It may trust a health
# report written by that daemon only while the daemon's own heartbeat names the
# same live PID. This is deliberately much shorter than the one-day health
# result age: a stopped process must not leave an armable console behind.
MAX_DAEMON_HEARTBEAT_AGE_DAYS = 5.0 / (24 * 60)


def arm_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / ARM_FILE


def read_arm(state_dir: Path | str) -> Optional[dict[str, Any]]:
    path = arm_path(state_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_armed(state_dir: Path | str) -> bool:
    payload = read_arm(state_dir)
    if not (payload and payload.get("armed")):
        return False
    # An arm file is a latch, not a permanent bypass. If the evidence becomes
    # stale, measured costs invalidate it, or the kill switch trips after the
    # file was written, submission must stop even before a console/auto-arm
    # process has had time to rewrite the file.
    allowed, _reason = _eligible_to_arm(state_dir)
    if not allowed:
        return False
    # The latch is only effective while *every* pre-flight check remains green.
    # In particular, an operator-written latch may stay on disk across a daemon
    # restart, but it cannot be used until that daemon has produced a fresh,
    # attributable back-check and published a current risk budget.
    return bool(evaluate(state_dir)["passed"])


def _eligible_to_arm(state_dir: Path | str) -> tuple[bool, str]:
    """Whether the system has reached the state that justifies arming."""
    def read(name: str) -> dict[str, Any]:
        try:
            payload = json.loads((Path(state_dir) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    promotion = read("promotion.json")
    last = promotion.get("last_assessment") or {}
    stage = str(promotion.get("stage") or "shadow")
    if stage == "shadow":
        return False, "the agent is still in shadow: it has not cleared its evidence gate"
    if not last.get("eligible"):
        return False, "the last assessment was not eligible on the evidence"
    evidence = read("strategy_evidence.json")
    stamp = str(evidence.get("generated_at") or "")
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False, "there is no walk-forward report to justify the size"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - seen).total_seconds() / 86_400
    if age_days > MAX_EVIDENCE_AGE_DAYS:
        return False, f"the evidence report is {age_days:.0f} days old"
    # A process upgrade can make an old report insufficient even while its
    # timestamp is fresh. The current schema includes account-level block
    # significance, doubled-cost stress, and exact fixed-dollar floor/daily-cap
    # parity; an older artifact must be rebuilt, not armed.
    from agentic_trading.evidence import EVIDENCE_SCHEMA_VERSION

    try:
        schema = int(evidence.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema < EVIDENCE_SCHEMA_VERSION:
        return False, "the walk-forward report uses an obsolete evidence schema"
    assessed = last.get("evidence") if isinstance(last, dict) else {}
    assessed = assessed if isinstance(assessed, dict) else {}
    if str(assessed.get("report_generated_at") or "") != stamp:
        return False, "the current walk-forward report has not been assessed"
    from agentic_trading.execution import unmodeled_round_trip

    gap = unmodeled_round_trip(state_dir, evidence.get("costs"))
    if gap is not None:
        asset, modeled, measured = gap
        return False, (
            f"the evidence models ${modeled:.2f} per {asset} round trip but "
            f"execution measured ${measured:.2f}"
        )
    guard = read("risk_guard.json")
    if guard.get("kill_switch"):
        return False, f"the kill switch is active: {guard.get('kill_reason') or 'unknown'}"
    return True, "the agent has cleared its evidence gate and the report is current"


def arm(state_dir: Path | str, *, source: str = "console") -> dict[str, Any]:
    """Record an arming request. Returns the payload that was written."""
    allowed, reason = _eligible_to_arm(state_dir)
    if not allowed:
        return {"armed": False, "refused": True, "reason": reason}
    verdict = evaluate(state_dir)
    if not verdict["passed"]:
        return {"armed": False, "refused": True, "reason": verdict["reason"]}
    payload = {
        "armed": True,
        "at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    _write(state_dir, payload)
    return payload


def disarm(state_dir: Path | str, *, source: str = "console", reason: str = "") -> dict[str, Any]:
    """Always allowed: lowering risk never needs permission."""
    previous = read_arm(state_dir) or {}
    payload = {
        "armed": False,
        "at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "reason": reason,
        "arms_today": int(previous.get("arms_today", 0) or 0),
        "arms_day": previous.get("arms_day", ""),
    }
    _write(state_dir, payload)
    return payload


def _write(state_dir: Path | str, payload: dict[str, Any]) -> None:
    from agentic_trading import jsonio

    jsonio.write_text(arm_path(state_dir), jsonio.dumps(payload, indent=2) + "\n")


def arm_status(state_dir: Path | str) -> dict[str, Any]:
    """For the console: may it be armed, is it armed, and why not."""
    allowed, reason = _eligible_to_arm(state_dir)
    if allowed:
        verdict = evaluate(state_dir)
        allowed = bool(verdict["passed"])
        reason = str(verdict["reason"])
    payload = read_arm(state_dir) or {}
    return {
        "armed": bool(payload.get("armed")) and allowed,
        "available": allowed,
        "reason": reason,
        "since": payload.get("at", ""),
        "source": payload.get("source", ""),
    }


# --- pre-flight, and the automatic arming that depends on it ----------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def preflight(state_dir: Path | str) -> list[Check]:
    """Everything that must be true before this account submits a real order.

    "After proper checking" is only meaningful if the checks are named, run, and
    reported — otherwise it is a hopeful phrase. Each one below is something the
    system already knows about itself; none of them requires a broker call, so
    the list can be shown on every console refresh.
    """
    base = Path(state_dir)

    def read(name: str) -> dict[str, Any]:
        try:
            payload = json.loads((base / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    checks: list[Check] = []
    promotion = read("promotion.json")
    last = promotion.get("last_assessment") or {}
    stage = str(promotion.get("stage") or "shadow")
    checks.append(
        Check(
            "promotion gate",
            bool(last.get("eligible")),
            f"last assessment eligible: {bool(last.get('eligible'))}",
        )
    )
    checks.append(
        Check("stage", stage in ("probation", "live"), f"stage is {stage}")
    )

    # Keep automatic arming on exactly the same gate as manual arming and the
    # runtime's per-order check. The stored promotion verdict can predate a new
    # execution-cost measurement; trusting it alone allowed auto-arm to write
    # an armed latch even though ``is_armed`` correctly refused to use it.
    currently_eligible, eligibility_reason = _eligible_to_arm(state_dir)
    checks.append(
        Check("current arming eligibility", currently_eligible, eligibility_reason)
    )

    evidence = read("strategy_evidence.json")
    age = _age_days(evidence.get("generated_at"))
    checks.append(
        Check(
            "evidence report",
            age is not None and age <= MAX_EVIDENCE_AGE_DAYS,
            "missing" if age is None else f"{age:.1f} days old",
        )
    )

    guard = read("risk_guard.json")
    checks.append(
        Check(
            "kill switch",
            not bool(guard.get("kill_switch")),
            str(guard.get("kill_reason") or "clear"),
        )
    )
    try:
        equity = float(guard.get("current_equity") or 0)
    except (TypeError, ValueError):
        equity = 0.0
    checks.append(
        Check("account readable", equity > 0, f"equity {equity:g}")
    )

    limits = read("effective_limits.json")
    details = limits.get("details") or {}
    try:
        in_force = float(limits.get("max_order_pct") or 0)
        # limits.py records the ceiling the budget was clamped to, so the check
        # compares the agent's own budget against the operator's own number.
        ceiling_pct = float(details.get("ceiling_max_order_pct") or in_force or 0)
    except (TypeError, ValueError):
        in_force, ceiling_pct = 0.0, 0.0
    checks.append(
        Check(
            "budget within ceiling",
            in_force > 0 and in_force <= ceiling_pct + 1e-12,
            f"{in_force:.4f} of {ceiling_pct:.4f}",
        )
    )

    health = read("health.json")
    checked_age = _age_days(health.get("finished_at"))
    # A back-check is evidence about this deployment only if the current daemon
    # wrote it. The daemon can compare the PID directly; the separate dashboard
    # proves the same fact through the daemon's fresh agents heartbeat. A CLI
    # self-check, stale process, test, or second workspace therefore cannot make
    # the account armable merely by leaving a healthy-looking JSON file behind.
    trusted_writer, writer_detail = _trusted_health_writer(base, health)
    checks.append(
        Check(
            "back-check",
            bool(health.get("healthy"))
            and checked_age is not None
            and checked_age <= 1.0
            and trusted_writer,
            "not run yet"
            if checked_age is None
            else (
                f"healthy={bool(health.get('healthy'))}, {checked_age * 24:.1f}h old"
                + (f" — {writer_detail}" if writer_detail else "")
            ),
        )
    )
    return checks


def _trusted_health_writer(
    state_dir: Path, health: dict[str, Any]
) -> tuple[bool, str]:
    """Prove that ``health.json`` came from this process or the live daemon."""
    try:
        writer = int(health.get("pid") or 0)
    except (TypeError, ValueError):
        writer = 0
    if writer <= 0:
        return False, "missing producer identity"
    if writer == os.getpid():
        return True, f"current process {writer}"

    try:
        agents_payload = json.loads(
            (state_dir / "agents.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False, "producer is not the current daemon"
    if not isinstance(agents_payload, dict):
        return False, "producer is not the current daemon"
    try:
        daemon_pid = int(agents_payload.get("pid") or 0)
    except (TypeError, ValueError):
        daemon_pid = 0
    heartbeat_age = _age_days(agents_payload.get("updated_at"))
    if daemon_pid != writer:
        return False, "producer differs from the daemon heartbeat"
    if (
        heartbeat_age is None
        or heartbeat_age < 0
        or heartbeat_age > MAX_DAEMON_HEARTBEAT_AGE_DAYS
    ):
        return False, "daemon heartbeat is stale"
    if not _pid_is_alive(writer):
        return False, "daemon process is no longer running"
    return True, f"current daemon process {writer}"


def _pid_is_alive(pid: int) -> bool:
    """Portable best-effort liveness check; permission denial still means alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _age_days(stamp: Any) -> Optional[float]:
    try:
        seen = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() / 86_400


def evaluate(state_dir: Path | str) -> dict[str, Any]:
    """The checklist, plus the verdict it implies."""
    checks = preflight(state_dir)
    failed = [check for check in checks if not check.ok]
    return {
        "checks": [check.to_dict() for check in checks],
        "passed": not failed,
        "failed": [check.name for check in failed],
        "reason": (
            "all checks passed"
            if not failed
            else "failing: " + ", ".join(check.name for check in failed)
        ),
    }


def maybe_auto_arm(
    state_dir: Path | str,
    *,
    enabled: bool,
    min_interval_hours: float = 6.0,
    max_arms_per_day: int = 6,
    clock: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """Arm without a human, but only while every check stays green.

    Returns the event to journal, or ``None`` when nothing changed. Two rules
    make this safe to leave running unattended:

    - it only arms on an all-green checklist, and re-checks on every call;
    - it disarms itself the moment a check fails, so a tripped kill switch, a
      stale report, a demotion or a failing back-check takes the account back
      out of the market without waiting for anyone.

    An arming the *operator* made is never touched: this only manages armings it
    made itself.
    """
    now = clock() if clock else datetime.now(timezone.utc)
    current = read_arm(state_dir) or {}
    armed = bool(current.get("armed"))
    source = str(current.get("source", ""))
    verdict = evaluate(state_dir)

    if armed and source == "auto" and not verdict["passed"]:
        payload = disarm(state_dir, source="auto", reason=verdict["reason"])
        return {"event": "auto_disarm", "reason": verdict["reason"], **payload}

    if not enabled:
        return None
    if armed:
        return None
    if not verdict["passed"]:
        return None

    # Rate-limit arming two ways. A short cooldown after a self-disarm (so a
    # recovered check does not leave the agent idle for hours), and a daily cap
    # (so something flapping between armed and disarmed is visible rather than
    # hammering the broker).
    previous_days = _age_days(current.get("at"))
    previous_hours = (previous_days or 0.0) * 24
    rearm_cooldown = min(0.25, min_interval_hours)
    was_auto_disarm = source == "auto" and not armed
    if previous_hours and previous_hours < (
        rearm_cooldown if was_auto_disarm else min_interval_hours
    ):
        return None
    today = now.date().isoformat()
    arms_today = int(current.get("arms_today", 0) or 0)
    if current.get("arms_day") != today:
        arms_today = 0
    if arms_today >= max_arms_per_day:
        return {
            "event": "auto_arm_refused",
            "reason": f"already armed {arms_today} times today",
            "arms_today": arms_today,
            "armed": False,
        }

    payload = _write_auto(state_dir, verdict)
    return {"event": "auto_arm", "reason": verdict["reason"], **payload}


def _write_auto(state_dir: Path | str, verdict: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    previous = read_arm(state_dir) or {}
    arms_today = int(previous.get("arms_today", 0) or 0)
    if previous.get("arms_day") != today:
        arms_today = 0
    payload = {
        "armed": True,
        "at": now.isoformat(),
        "source": "auto",
        "checks": verdict["checks"],
        "arms_today": arms_today + 1,
        "arms_day": today,
    }
    _write(state_dir, payload)
    return payload
