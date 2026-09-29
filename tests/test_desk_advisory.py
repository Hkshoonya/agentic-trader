"""The desk's account orders follow the evidence; the advisory layers only watch.

The allocator funds a member because its paper book, traded without any veto,
beat buy-and-hold. If the LLM veto, the regime gate or Jev's chase check could
then refuse the account's orders, the money would hold something other than
what earned it, and nobody could tell whether the veto helped. So for
``desk_rebalance`` orders each layer is still asked and its opinion journaled,
but it never blocks. Every other entry is vetoed exactly as before.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agentic_trading.broker import Broker
from agentic_trading.config import load_config
from agentic_trading.llm.advisor import AdvisorDecision
from agentic_trading.llm.regime import RegimeView
from agentic_trading.runtime import _Loop
from agentic_trading.types import OrderIntent, Side
from tests.fakes import FakeMcpClient
from tests.test_crypto_routing import (
    FIXED_NOW,
    _crypto_quote,
    _records,
    _write_config,
    load_tools,
)


def _intent(reason: str) -> OrderIntent:
    quote = _crypto_quote()
    return OrderIntent(
        decision_id=f"{reason}-1",
        symbol="BTC-USD",
        side=Side.BUY,
        quantity=Decimal("0.0002"),
        ref_price=quote["ask"],
        reason=reason,
        created_at=FIXED_NOW,
    )


class _VetoAdvisor:
    model = "stub"
    last_reused = False
    last_error = ""

    def review_entry(self, **_kwargs):
        return AdvisorDecision(
            action="veto", confidence=0.9, hold=False, reason="counter-trend"
        )


class _ChopGate:
    model = "stub"

    def blocks(self, symbol: str) -> RegimeView:
        return RegimeView(
            symbol=symbol,
            regime="chop",
            confidence=0.8,
            reason="range-bound",
            at="2026-09-16T14:00:00+00:00",
        )


class _ChasingJev:
    model = "stub"

    def view(self, symbol: str) -> dict:
        return {"chase": 0.95}


def _process(layer: str, reason: str) -> list[dict]:
    tools = load_tools()
    broker = Broker(FakeMcpClient(tools, equity="1000"), tools)
    with tempfile.TemporaryDirectory() as name:
        config = load_config(_write_config(Path(name)))
        if layer == "jev":
            config = dataclasses.replace(config, jev_veto_chase=True)
        loop = _Loop(config, broker, None)
        loop.start()
        if layer == "llm":
            loop.advisor = _VetoAdvisor()
        elif layer == "regime":
            loop.regime_gate = _ChopGate()
        else:
            loop.entry_advisor = _ChasingJev()
        loop.process_intent(_intent(reason))
        return _records(config)


def _outcome(records: list[dict]) -> str:
    events = [r["event"] for r in records if r.get("event") in ("accepted", "rejected")]
    return events[-1] if events else "none"


class DeskOrdersAreNotVetoedTests(unittest.TestCase):
    def test_every_advisory_layer_still_refuses_an_ordinary_entry(self) -> None:
        for layer in ("llm", "regime", "jev"):
            with self.subTest(layer=layer):
                self.assertEqual(_outcome(_process(layer, "trend_entry")), "rejected")

    def test_no_advisory_layer_refuses_a_desk_order(self) -> None:
        for layer in ("llm", "regime", "jev"):
            with self.subTest(layer=layer):
                records = _process(layer, "desk_rebalance")
                self.assertEqual(_outcome(records), "accepted")
                noted = [r for r in records if r.get("event") == "advisory_overruled"]
                self.assertEqual(len(noted), 1, records)
                self.assertEqual(noted[0]["layer"], layer)
                self.assertEqual(noted[0]["symbol"], "BTC-USD")
                self.assertTrue(noted[0]["opinion"])

    def test_the_llm_opinion_on_a_desk_order_is_still_journaled(self) -> None:
        records = _process("llm", "desk_rebalance")
        advisor = [r for r in records if r.get("event") == "advisor"]
        self.assertEqual(len(advisor), 1)
        self.assertEqual(advisor[0]["action"], "veto")


if __name__ == "__main__":
    unittest.main()
