"""Operator alerts for the handful of events that actually need a human.

The daemon is built to run unattended, which is exactly why the events that
change its risk posture have to reach the operator instead of waiting to be
noticed on a dashboard tab:

- the evidence gate passed and the agent moved its own stage
- it wants to place an order but the arming switch is off
- the kill switch tripped, or a demotion dropped it back to shadow
- a real order was submitted

Design rules, same spirit as the rest of the advisory layer:

- **Never block trading.** A failed alert is swallowed and counted; the loop
  keeps running. ``notify-send`` on a headless box, a dead Telegram token, a
  five-second timeout — none of these may cost a decision.
- **Never spam.** Alerts are deduplicated by key with a per-kind cooldown, so a
  blocked entry every six seconds becomes one message an hour.
- **Deaf by default in tests**, enabled in real runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

# Keys are journal event names; values are (cooldown seconds, urgency).
# A cooldown of 0 means "every occurrence is worth a message".
ALERT_EVENTS: dict[str, tuple[float, str]] = {
    "promotion": (0.0, "normal"),
    "promotion_requires_consent": (0.0, "normal"),
    "live_gate_blocked": (3600.0, "normal"),
    "kill_switch": (0.0, "critical"),
    "demotion": (0.0, "critical"),
    "placed": (0.0, "normal"),
}


@dataclass
class Alert:
    key: str
    title: str
    body: str
    urgency: str = "normal"
    channels: list[str] = field(default_factory=list)
    delivered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "body": self.body,
            "urgency": self.urgency,
            "channels": list(self.channels),
            "delivered": self.delivered,
        }


class Channel(Protocol):
    name: str

    def send(self, alert: Alert) -> bool: ...


def _money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def alert_for(record: dict[str, Any]) -> Optional[Alert]:
    """Map a journal record to an alert, or ``None`` when it is routine."""
    event = str(record.get("event") or "")
    if event not in ALERT_EVENTS:
        return None
    _, urgency = ALERT_EVENTS[event]

    if event == "promotion":
        stage = record.get("to") or record.get("stage") or "?"
        return Alert(
            key=f"promotion:{stage}",
            title=f"Trading agent promoted to {stage}",
            body=(
                f"It cleared its evidence gate and moved itself "
                f"{record.get('from', '?')} → {stage}."
            ),
            urgency=urgency,
        )
    if event == "promotion_requires_consent":
        return Alert(
            key="consent",
            title="Evidence gate passed, but self-promotion is disabled",
            body=str(record.get("hint") or "autonomy is not set to auto"),
            urgency=urgency,
        )
    if event == "live_gate_blocked":
        return Alert(
            key=f"gate:{record.get('symbol', '?')}",
            title="Order blocked: live trading is not armed",
            body=(
                f"{record.get('symbol', '?')} {record.get('side', '?')} "
                f"{_money(record.get('notional'))} passed every check and was "
                f"not submitted — AGENTIC_ALLOW_LIVE is off."
            ),
            urgency=urgency,
        )
    if event == "kill_switch":
        return Alert(
            key="kill",
            title="Kill switch tripped",
            body=(
                f"Reason: {record.get('reason', 'unknown')}. The agent has "
                f"stopped opening positions and needs a look."
            ),
            urgency=urgency,
        )
    if event == "demotion":
        return Alert(
            key="demotion",
            title="Agent demoted itself",
            body=(
                f"{record.get('from', '?')} → {record.get('to', 'shadow')} · "
                f"{record.get('reason', '')}"
            ),
            urgency=urgency,
        )
    if event == "placed":
        request = record.get("order_request") if isinstance(record, dict) else {}
        request = request if isinstance(request, dict) else {}
        return Alert(
            key=f"placed:{record.get('decision_id', '')}",
            title="Order placed",
            body=(
                f"{request.get('symbol', '?')} {request.get('side', '?')} "
                f"{request.get('quantity') or _money(request.get('dollar_amount'))}"
            ),
            urgency=urgency,
        )
    return None


class DesktopChannel:
    """Linux desktop notification via ``notify-send``."""

    name = "desktop"

    def __init__(self, binary: str = "notify-send", timeout: float = 5.0) -> None:
        self.binary = binary
        self.timeout = timeout

    def send(self, alert: Alert) -> bool:
        try:
            result = subprocess.run(
                [
                    self.binary,
                    "-u",
                    "critical" if alert.urgency == "critical" else "normal",
                    "-a",
                    "Agentic Trader",
                    alert.title,
                    alert.body,
                ],
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0


class TelegramChannel:
    """Telegram bot message; needs ``AGENTIC_TELEGRAM_*`` in the environment."""

    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: float = 5.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, alert: Alert) -> bool:
        import httpx

        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": f"{alert.title}\n{alert.body}",
                    "disable_notification": alert.urgency != "critical",
                },
                timeout=self.timeout,
            )
        except Exception:  # noqa: BLE001 — alerts must never break the loop
            return False
        return response.status_code == 200


class Notifier:
    """Deduplicating fan-out over the configured channels."""

    def __init__(
        self,
        channels: list[Channel],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.channels = list(channels)
        self._clock = clock
        self._last_sent: dict[str, float] = {}
        self.sent = 0
        self.failures = 0
        # Per-channel failures are worth keeping: "desktop is dead but Telegram
        # works" is exactly the kind of thing an operator needs to know.
        self.channel_failures: dict[str, int] = {}

    def cooldown_for(self, key: str) -> float:
        event = key.split(":", 1)[0]
        if event == "gate":
            return ALERT_EVENTS["live_gate_blocked"][0]
        return ALERT_EVENTS.get(event, (0.0, ""))[0]

    def _cooling_down(self, key: str, now: float) -> bool:
        cooldown = self.cooldown_for(key)
        if cooldown <= 0:
            return False
        last = self._last_sent.get(key)
        return last is not None and (now - last) < cooldown

    def dispatch(self, record: dict[str, Any]) -> Optional[Alert]:
        """Alert on a journal record. Returns the alert when one was sent."""
        alert = alert_for(record)
        if alert is None or not self.channels:
            return None
        now = self._clock().timestamp()
        if self._cooling_down(alert.key, now):
            return None

        for channel in self.channels:
            try:
                delivered = channel.send(alert)
            except Exception:  # noqa: BLE001 — one bad channel is not fatal
                delivered = False
            if delivered:
                alert.channels.append(channel.name)
                alert.delivered = True
            else:
                self.channel_failures[channel.name] = (
                    self.channel_failures.get(channel.name, 0) + 1
                )
        if alert.delivered:
            self.sent += 1
            self._last_sent[alert.key] = now
        else:
            self.failures += 1
        return alert


def build_notifier() -> Optional[Notifier]:
    """The notifier a real run gets: desktop if available, Telegram if set."""
    from agentic_trading.llm.client import load_dotenv, running_under_tests

    if running_under_tests():
        return None
    load_dotenv()
    if os.environ.get("AGENTIC_NOTIFY", "1") == "0":
        return None

    channels: list[Channel] = []
    if shutil.which("notify-send"):
        channels.append(DesktopChannel())
    token = os.environ.get("AGENTIC_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("AGENTIC_TELEGRAM_CHAT_ID")
    if token and chat_id:
        channels.append(TelegramChannel(token, chat_id))
    return Notifier(channels) if channels else None
