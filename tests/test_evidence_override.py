"""A size above the evidence frontier needs the operator to say so.

The size schedule the operator asked for is deliberately bigger than the
drawdown evidence supports — 22% per order on a $50 account where the measured
frontier holds 3% inside its 15% ceiling. Two wrong ways to resolve that: let the
schedule quietly pass the gate, or let the gate block the operator's own
instruction. The right way is a named switch that costs something to set, a note
that travels with every verdict, and a console that prints the trade-off.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agentic_trading.promotion import PromotionPolicy, assess_walkforward


def _report(*, gate_pct: float = 0.03, bars: int = 30_000) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drawdown_ceiling_pct": 15.0,
        "series": {"symbols": ["SPY", "BTC-USD"], "bars": bars},
        "configs": {
            "production": {
                "per_order_pct": 0.2226,
                "trades": 495,
                "expectancy_bps": 856.0,
                "win_rate": 0.5,
                "profit_factor": 3.7,
                "max_drawdown_pct": 4.1,
                # A real report carries the bootstrap verdict; without it the
                # assessment (rightly) treats the edge as indistinguishable from
                # noise and nothing else in this test would be reached.
                "bootstrap_p_value": 0.0005,
                # Folds live inside the production config, the way the real
                # report writes them.
                "folds": [
                    {"trades": 80, "expectancy_bps": 120.0, "max_drawdown_pct": 3.0}
                ]
                * 6,
            }
        },
        "gate_size": {"per_order_pct": gate_pct},
    }


def _assess(**kwargs):
    return assess_walkforward(
        _report(),
        PromotionPolicy(min_oos_trades=1, required_cycles=1),
        live_per_order_pct=0.2226,
        **kwargs,
    )


class EvidenceOverrideTests(unittest.TestCase):
    def test_without_the_switch_the_size_blocks_promotion(self) -> None:
        verdict = _assess()
        self.assertFalse(verdict.eligible)
        self.assertTrue(
            any("only supports" in reason for reason in verdict.reasons),
            verdict.reasons,
        )
        self.assertEqual(verdict.notes, [])

    def test_with_the_switch_the_trade_off_becomes_a_note(self) -> None:
        verdict = assess_walkforward(
            _report(),
            PromotionPolicy(
                min_oos_trades=1,
                required_cycles=1,
                accept_evidence_override=True,
            ),
            live_per_order_pct=0.2226,
        )
        self.assertTrue(verdict.eligible, verdict.reasons)
        self.assertTrue(
            any("only supports" in note for note in verdict.notes), verdict.notes
        )
        # The note has to survive into the payload the console reads.
        payload = verdict.to_dict()
        self.assertEqual(len(payload["notes"]), 1)
        self.assertIn("22.26%", payload["notes"][0])

    def test_a_size_inside_the_frontier_needs_no_switch(self) -> None:
        verdict = assess_walkforward(
            _report(gate_pct=0.25),
            PromotionPolicy(min_oos_trades=1, required_cycles=1),
            live_per_order_pct=0.2226,
        )
        self.assertTrue(verdict.eligible, verdict.reasons)
        self.assertEqual(verdict.notes, [])


if __name__ == "__main__":
    unittest.main()
