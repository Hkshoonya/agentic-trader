"""LLM regime gate: strategic context, bounded to reducing risk."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_trading.llm.advisor import LlmAdvisor
from agentic_trading.llm.market import (
    MarketFeatures,
    context_lines,
    features_for,
    features_from_closes,
)
from agentic_trading.llm.regime import RegimeGate, parse_regime


class _RecordingClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.model = "recording"

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return self.reply


class MarketFeatureTests(unittest.TestCase):
    def test_features_describe_trend_volatility_and_range(self) -> None:
        closes = [100.0 * (1.01**i) for i in range(40)]
        features = features_from_closes("BTC-USD", closes, spread_bps=12.5)
        assert features is not None
        self.assertEqual(features.bars, 40)
        self.assertGreater(features.ret_20_pct, 0)
        self.assertGreater(features.trend_pct, 0)
        self.assertAlmostEqual(features.range_position, 1.0, places=6)
        self.assertEqual(features.spread_bps, 12.5)
        self.assertIn("trend_slope_20bar_pct", " ".join(features.describe()))

    def test_too_few_bars_yields_no_features(self) -> None:
        self.assertIsNone(features_from_closes("BTC-USD", [100.0, 101.0]))

    def test_features_come_from_real_bar_files(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            rows = [
                json.dumps(
                    {
                        "symbol": "BTCUSD",
                        "start": f"2026-01-{i + 1:02d}T00:00:00+00:00",
                        "open": str(100 + i),
                        "high": str(101 + i),
                        "low": str(99 + i),
                        "close": str(100 + i),
                        "volume": "10",
                    }
                )
                for i in range(30)
            ]
            (tmp / "BTCUSD_day.jsonl").write_text("\n".join(rows) + "\n")
            features = features_for("BTC-USD", history_path=tmp)
        self.assertIsNotNone(features)
        assert features is not None
        self.assertEqual(features.bars, 30)

    def test_volume_features_are_reported_when_the_data_is_real(self) -> None:
        closes = [100.0 + i for i in range(40)]
        volumes = [10.0] * 39 + [80.0]
        features = features_from_closes("BTC-USD", closes, volumes=volumes)
        self.assertIsNotNone(features.volume_z)
        self.assertGreater(features.volume_z, 1.0)
        # The spike is inside the 5-bar window, so the ratio is above 1.
        self.assertGreater(features.volume_trend, 1.0)
        self.assertIn("volume_z_score", " ".join(features.describe()))

    def test_volume_is_dropped_when_the_column_is_mostly_zeros(self) -> None:
        """A z-score on an artefact column is noise dressed as activity."""
        closes = [100.0 + i for i in range(40)]
        volumes = [0.0] * 35 + [10.0] * 5  # only 12% coverage
        features = features_from_closes("SOL-USD", closes, volumes=volumes)
        self.assertIsNone(features.volume_z)
        self.assertLess(features.volume_coverage, 0.8)
        text = " ".join(features.describe())
        self.assertIn("volume=unreliable", text)
        self.assertNotIn("volume_z_score", text)

    def test_spread_is_taken_from_the_live_quote(self) -> None:
        closes = [100.0 + i for i in range(30)]
        quote = {"bid": "99.99", "ask": "100.01"}
        from agentic_trading.llm.market import features_from_closes as build

        base = build("BTC-USD", closes)
        self.assertIsNone(base.spread_bps)
        with tempfile.TemporaryDirectory() as name:
            tmp = Path(name)
            (tmp / "BTCUSD_day.jsonl").write_text(
                "\n".join(
                    json.dumps({"start": f"t{i}", "close": str(100 + i)})
                    for i in range(30)
                )
                + "\n"
            )
            features = features_for("BTC-USD", history_path=tmp, quote=quote)
        self.assertAlmostEqual(features.spread_bps, 2.0, places=1)


class PromptGroundingTests(unittest.TestCase):
    def test_advisor_prompt_carries_the_tape(self) -> None:
        client = _RecordingClient(
            '{"action":"allow","confidence":0.5,"hold":false,"reason":"ok"}'
        )
        advisor = LlmAdvisor(client, model="recording")
        features = features_from_closes(
            "BTC-USD", [100.0 + i for i in range(40)]
        )
        advisor.review_entry(
            symbol="BTC-USD",
            side="buy",
            ref_price="139",
            quantity="0.01",
            reason="trend",
            context={"market": features, "session": "regular"},
        )
        prompt = client.prompts[0]
        self.assertIn("market_context", prompt)
        self.assertIn("realized_vol_per_bar_pct", prompt)
        self.assertIn("session=regular", prompt)
        self.assertNotIn("market=", prompt)  # the object itself is not dumped

    def test_missing_bars_are_stated_not_omitted(self) -> None:
        self.assertEqual(context_lines(None), ["market_context=unavailable"])


class RegimeParsingTests(unittest.TestCase):
    def test_parses_a_valid_classification(self) -> None:
        view = parse_regime(
            '{"regime":"chop","confidence":0.8,"reason":"range"}', symbol="BTC-USD"
        )
        assert view is not None
        self.assertTrue(view.blocks_entries)
        self.assertEqual(view.symbol, "BTC-USD")

    def test_rejects_unknown_regimes_and_garbage(self) -> None:
        self.assertIsNone(parse_regime('{"regime":"moon"}', symbol="BTC-USD"))
        self.assertIsNone(parse_regime("not json", symbol="BTC-USD"))
        self.assertIsNone(parse_regime("", symbol="BTC-USD"))

    def test_code_fences_are_tolerated(self) -> None:
        view = parse_regime(
            '```json\n{"regime":"trend_up","confidence":0.7}\n```', symbol="SPY"
        )
        assert view is not None
        self.assertFalse(view.blocks_entries)


class RegimeGateTests(unittest.TestCase):
    def _gate(self, reply: str, *, block_confidence: float = 0.6) -> RegimeGate:
        return RegimeGate(
            _RecordingClient(reply), block_confidence=block_confidence
        )

    def test_a_confident_chop_blocks_entries(self) -> None:
        gate = self._gate('{"regime":"chop","confidence":0.8,"reason":"range-bound"}')
        gate.refresh("BTC-USD", None)
        blocked = gate.blocks("BTC-USD")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.regime, "chop")

    def test_a_trend_never_creates_an_entry(self) -> None:
        gate = self._gate('{"regime":"trend_up","confidence":0.95}')
        gate.refresh("BTC-USD", None)
        self.assertIsNone(gate.blocks("BTC-USD"))

    def test_a_low_confidence_chop_does_not_block(self) -> None:
        gate = self._gate('{"regime":"chop","confidence":0.4}')
        gate.refresh("BTC-USD", None)
        self.assertIsNone(gate.blocks("BTC-USD"))

    def test_unknown_symbol_has_no_opinion(self) -> None:
        gate = self._gate('{"regime":"panic","confidence":1.0}')
        self.assertIsNone(gate.blocks("BTC-USD"))

    def test_views_expire_after_the_ttl(self) -> None:
        now = datetime.now(timezone.utc)
        gate = RegimeGate(
            _RecordingClient('{"regime":"trend_up","confidence":0.9}'),
            ttl_seconds=600,
            clock=lambda: now,
        )
        gate.refresh("SPY", None)
        self.assertFalse(gate.due("SPY"))
        later = RegimeGate(
            _RecordingClient("{}"),
            ttl_seconds=600,
            clock=lambda: now + timedelta(seconds=601),
        )
        later._views.update(gate._views)  # noqa: SLF001 — simulate elapsed time
        self.assertTrue(later.due("SPY"))

    def test_a_model_failure_is_not_a_block(self) -> None:
        class _Broken:
            model = "broken"

            def complete(self, system: str, user: str) -> str:
                raise RuntimeError("gateway down")

        gate = RegimeGate(_Broken())
        self.assertIsNone(gate.refresh("BTC-USD", None))
        self.assertIsNone(gate.blocks("BTC-USD"))
        self.assertIn("gateway down", gate.last_error)

    def test_refresh_due_is_rate_limited_per_pass(self) -> None:
        gate = self._gate('{"regime":"trend_up","confidence":0.9}')
        refreshed = gate.refresh_due(
            ["BTC-USD", "ETH-USD", "SOL-USD"], lambda symbol: None, max_per_pass=2
        )
        self.assertEqual(len(refreshed), 2)
        self.assertEqual(len(gate.views()), 2)

    def test_features_are_passed_to_the_model(self) -> None:
        client = _RecordingClient('{"regime":"trend_up","confidence":0.9}')
        gate = RegimeGate(client)
        features = features_from_closes("BTC-USD", [100.0 + i for i in range(30)])
        gate.refresh("BTC-USD", features)
        self.assertIn("trend_slope_20bar_pct", client.prompts[0])


if __name__ == "__main__":
    unittest.main()
