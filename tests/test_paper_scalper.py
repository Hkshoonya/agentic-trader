"""Behavioral tests for the local, quote-driven paper experiment.

All observations are synthetic. No network or brokerage access is needed.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from paper_scalper import Config, run_experiment


D = Decimal
BASE = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)


def timestamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def quote(seconds, bid="100", ask=None, *, quote_seconds=None):
    bid = str(bid)
    return {
        "symbol": "SPY",
        "observed_at": timestamp(seconds),
        "quote_at": timestamp(seconds if quote_seconds is None else quote_seconds),
        "bid": bid,
        "ask": str(D(bid) + D("0.01")) if ask is None else str(ask),
    }


def entry_quotes(start=0):
    return [
        quote(start, "100"),
        quote(start + 1, "100.01"),
        quote(start + 2, "100.02"),
    ]


class PaperScalperTests(unittest.TestCase):
    def assert_money_equal(self, actual, expected):
        self.assertLessEqual(abs(D(str(actual)) - expected), D("0.000001"))

    def test_empty_and_insufficient_signals_preserve_cash(self):
        for observations in (
            [],
            entry_quotes()[:2],
            [quote(i, "100") for i in range(5)],
            [quote(i, str(D("100.04") - D(i) / 100)) for i in range(5)],
        ):
            with self.subTest(observations=observations):
                result = run_experiment(observations)
                self.assertEqual(result["trades"], [])
                self.assertIsNone(result["open_position"])
                self.assert_money_equal(result["ending_equity"], D("50"))

    def test_open_position_uses_liquidation_value_without_fabricated_exit(self):
        config = Config(fee_per_order=D("0.10"))
        result = run_experiment(entry_quotes(), config)

        self.assertEqual(result["trades"], [])
        self.assertEqual(D(result["realized_pnl"]), D("0"))
        position = result["open_position"]
        self.assertIsInstance(position, dict)
        quantity = D(position["quantity"])
        entry_price = D(position["entry_price"])
        self.assertEqual(entry_price, D("100.03") * D("1.0001"))
        cost = quantity * entry_price + config.fee_per_order
        self.assertGreater(quantity, D("0"))
        self.assertLessEqual(cost, config.initial_cash)

        liquidation_value = quantity * D("100.02") * D("0.9999") - config.fee_per_order
        self.assert_money_equal(position["liquidation_value"], liquidation_value)
        self.assert_money_equal(result["ending_equity"], config.initial_cash - cost + liquidation_value)
        self.assertLess(D(result["ending_equity"]), D("50"))

    def test_timeout_round_trip_conserves_cash_after_both_fees_and_slippage(self):
        config = Config(
            fee_per_order=D("0.10"),
            take_profit_bps=D("1000"),
            stop_loss_bps=D("1000"),
        )
        result = run_experiment(entry_quotes() + [quote(62, "100.02")], config)

        self.assertIsNone(result["open_position"])
        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "timeout")
        quantity = D(trade["quantity"])
        entry_price, exit_price = D(trade["entry_price"]), D(trade["exit_price"])
        self.assertEqual(entry_price, D("100.03") * D("1.0001"))
        self.assertEqual(exit_price, D("100.02") * D("0.9999"))
        self.assertLessEqual(quantity * entry_price + config.fee_per_order, config.initial_cash)

        pnl = quantity * (exit_price - entry_price) - 2 * config.fee_per_order
        self.assert_money_equal(trade["net_pnl"], pnl)
        self.assert_money_equal(result["realized_pnl"], pnl)
        self.assert_money_equal(result["ending_equity"], config.initial_cash + pnl)
        self.assertLess(pnl, -2 * config.fee_per_order)

    def test_profit_and_stop_exits_use_executable_prices(self):
        for bid, reason in (("100.30", "take_profit"), ("99.80", "stop_loss")):
            with self.subTest(reason=reason):
                result = run_experiment(entry_quotes() + [quote(3, bid)])
                self.assertEqual(len(result["trades"]), 1)
                trade = result["trades"][0]
                self.assertEqual(trade["exit_reason"], reason)
                self.assertEqual(D(trade["exit_price"]), D(bid) * D("0.9999"))
                self.assertIsNone(result["open_position"])

    def test_midpoint_gain_does_not_count_as_profit_after_execution_costs(self):
        # The bid has risen 0.12% from the initial quote, but the entry ask
        # and two slippage charges leave the position below its 0.10% target.
        result = run_experiment(entry_quotes() + [quote(3, "100.12")])
        self.assertEqual(result["trades"], [])
        self.assertIsInstance(result["open_position"], dict)

    def test_fees_can_trigger_net_loss_stop_despite_a_rising_market(self):
        result = run_experiment(
            entry_quotes() + [quote(3, "100.06")],
            Config(fee_per_order=D("0.10")),
        )
        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_reason"], "stop_loss")
        self.assertGreater(D(trade["exit_price"]), D(trade["entry_price"]))
        self.assertLess(D(trade["net_pnl"]), D("-0.05"))

    def test_exit_fee_reserve_prevents_borrowing_after_a_severe_price_gap(self):
        result = run_experiment(
            entry_quotes() + [quote(3, "0.0001", "0.0002")],
            Config(fee_per_order=D("0.10")),
        )
        self.assertEqual(len(result["trades"]), 1)
        self.assertIsNone(result["open_position"])
        self.assertGreaterEqual(D(result["ending_equity"]), D("0"))
        self.assertGreaterEqual(D(result["realized_pnl"]), D("-50"))

    def test_timeout_uses_decision_time_and_waits_for_full_holding_period(self):
        observations = entry_quotes() + [quote(61, "100.02", quote_seconds=60)]
        still_open = run_experiment(observations)
        self.assertEqual(still_open["trades"], [])

        # Market data is one second old and remains fresh. At observation
        # time 62 the holding period is 60 seconds, even though quote time is 61.
        result = run_experiment(observations + [quote(62, "100.02", quote_seconds=61)])
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "timeout")

    def test_stale_quote_cannot_mark_or_close_an_existing_position(self):
        baseline = run_experiment(entry_quotes())
        result = run_experiment(entry_quotes() + [quote(62, "110", quote_seconds=51)])
        self.assertEqual(result["rejected_quotes"], 1)
        self.assertEqual(result["trades"], [])
        self.assertIsInstance(result["open_position"], dict)
        self.assertEqual(result["ending_equity"], baseline["ending_equity"])

    def test_wide_spreads_block_entry_but_allow_risk_exit(self):
        wide = [quote(i, str(D("100") + i), str(D("100.10") + i)) for i in range(3)]
        result = run_experiment(wide)
        self.assertEqual(result["accepted_quotes"], 3)
        self.assertEqual(result["rejected_quotes"], 0)
        self.assertIsNone(result["open_position"])

        result = run_experiment(entry_quotes() + [quote(3, "99.80", "100.20")])
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "stop_loss")

    def test_invalid_quotes_are_rejected_without_spending_cash(self):
        invalid = [
            quote(0, "0", "1"),
            quote(1, "101", "100"),
            quote(2, "NaN", "100"),
            quote(3, "100", "Infinity"),
            quote(4, "-1", "1"),
            quote(5, "100", "100.01", quote_seconds=6),
        ]
        missing_ask = quote(6, "100")
        del missing_ask["ask"]
        invalid.append(missing_ask)
        result = run_experiment(invalid)
        self.assertEqual(result["rejected_quotes"], len(invalid))
        self.assertEqual(result["accepted_quotes"], 0)
        self.assertIsNone(result["open_position"])
        self.assert_money_equal(result["ending_equity"], D("50"))

    def test_duplicate_and_out_of_order_quotes_cannot_create_momentum(self):
        for observations in (
            [quote(2, "100"), quote(1, "100.01"), quote(0, "100.02")],
            [quote(0, "100"), quote(1, "100.01", quote_seconds=0), quote(2, "100.02", quote_seconds=0)],
        ):
            with self.subTest(observations=observations):
                result = run_experiment(observations)
                self.assertEqual(result["accepted_quotes"], 1)
                self.assertEqual(result["rejected_quotes"], 2)
                self.assertIsNone(result["open_position"])

    def test_rejected_and_wide_quotes_reset_entry_signal(self):
        for disruption in (
            quote(2, "100.02", quote_seconds=-9),
            quote(2, "100.02", "100.12"),
        ):
            with self.subTest(disruption=disruption):
                result = run_experiment(entry_quotes()[:2] + [disruption, quote(3, "100.03")])
                self.assertEqual(result["trades"], [])
                self.assertIsNone(result["open_position"])

    def test_cooldown_prevents_immediate_reentry(self):
        observations = entry_quotes() + [quote(3, "100.30")]
        observations += [quote(10 + i, str(D("100.31") + D(i) / 100)) for i in range(3)]
        result = run_experiment(observations)
        self.assertEqual(len(result["trades"]), 1)
        self.assertIsNone(result["open_position"])

        observations += entry_quotes(start=33) + [quote(36, "100.30")]
        result = run_experiment(observations)
        self.assertEqual(len(result["trades"]), 2)
        self.assertIsNone(result["open_position"])

    def test_maximum_completed_trades_prevents_further_entries(self):
        observations = []
        for cycle in range(4):
            observations += entry_quotes(start=cycle * 4)
            observations.append(quote(cycle * 4 + 3, "100.30"))
        result = run_experiment(observations, Config(cooldown_seconds=0))
        self.assertEqual(len(result["trades"]), 3)
        self.assertIsNone(result["open_position"])
        total_pnl = sum((D(trade["net_pnl"]) for trade in result["trades"]), D("0"))
        self.assert_money_equal(result["ending_equity"], D("50") + total_pnl)

    def test_loss_limit_closes_position_and_stops_further_entries(self):
        config = Config(
            daily_loss_limit=D("0.05"),
            stop_loss_bps=D("1000"),
            cooldown_seconds=0,
        )
        observations = entry_quotes() + [quote(3, "99.90")]
        observations += entry_quotes(start=4) + [quote(7, "100.30")]
        result = run_experiment(observations, config)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "loss_limit")
        self.assertLessEqual(D(result["ending_equity"]), D("49.95"))
        self.assertIsNone(result["open_position"])

    def test_untrusted_config_is_rejected_before_processing_quotes(self):
        invalid_values = {
            "initial_cash": [D("0"), D("-1"), D("NaN"), D("Infinity")],
            "max_spread_bps": [D("0"), D("-1"), D("NaN")],
            "slippage_bps": [D("-1"), D("NaN"), D("10000"), D("10001")],
            "fee_per_order": [D("-1"), D("Infinity"), D("25")],
            "take_profit_bps": [D("0"), D("-1"), D("NaN")],
            "stop_loss_bps": [D("0"), D("-1"), D("NaN")],
            "daily_loss_limit": [D("0"), D("-1"), D("NaN")],
            "max_hold_seconds": [0, -1, 1.5, True],
            "cooldown_seconds": [-1, 1.5],
            "max_trades": [0, -1, 1.5],
            "max_quote_age_seconds": [0, -1],
            "max_signal_gap_seconds": [0, -1],
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises((ValueError, TypeError)):
                        run_experiment([], Config(**{field: value}))

    def test_mixed_symbol_cannot_complete_entry_signal(self):
        other_security = quote(2, "100.02")
        other_security["symbol"] = "AAPL"
        result = run_experiment(entry_quotes()[:2] + [other_security, quote(3, "100.03")])
        self.assertEqual(result["rejected_quotes"], 1)
        self.assertEqual(result["trades"], [])
        self.assertIsNone(result["open_position"])

    def test_mixed_symbol_cannot_mark_or_liquidate_position(self):
        baseline = run_experiment(entry_quotes())
        other_security = quote(3, "110")
        other_security["symbol"] = "AAPL"
        result = run_experiment(entry_quotes() + [other_security])
        self.assertEqual(result["rejected_quotes"], 1)
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["ending_equity"], baseline["ending_equity"])

        result = run_experiment(entry_quotes() + [other_security, quote(4, "99.80")])
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "stop_loss")

    def test_configured_symbol_can_trade_without_default_symbol(self):
        observations = entry_quotes() + [quote(3, "100.30")]
        for observation in observations:
            observation["symbol"] = "QQQ"
        result = run_experiment(observations, Config(symbol="QQQ"))
        self.assertEqual(result["accepted_quotes"], 4)
        self.assertEqual(result["rejected_quotes"], 0)
        self.assertEqual(len(result["trades"]), 1)

    def test_gap_exceeding_signal_limit_requires_three_new_observations(self):
        config = Config(max_signal_gap_seconds=30)
        observations = entry_quotes()[:2] + [quote(32, "100.02"), quote(33, "100.03")]
        result = run_experiment(observations, config)
        self.assertIsNone(result["open_position"])

        result = run_experiment(observations + [quote(34, "100.04")], config)
        self.assertIsInstance(result["open_position"], dict)

    def test_gap_at_signal_limit_keeps_recent_momentum(self):
        result = run_experiment(
            entry_quotes()[:2] + [quote(31, "100.02")],
            Config(max_signal_gap_seconds=30),
        )
        self.assertIsInstance(result["open_position"], dict)

    def test_increasing_source_time_cannot_override_reversed_observation_time(self):
        observations = [
            quote(10, "100", quote_seconds=0),
            quote(9, "100.01", quote_seconds=1),
            quote(8, "100.02", quote_seconds=2),
        ]
        result = run_experiment(observations)
        self.assertEqual(result["accepted_quotes"], 1)
        self.assertEqual(result["rejected_quotes"], 2)
        self.assertIsNone(result["open_position"])

    def test_rejected_market_data_still_advances_observation_clock(self):
        observations = [
            quote(0, "100"),
            quote(12, "100.01", quote_seconds=1),
            quote(2, "100.02"),
            quote(3, "100.03"),
        ]
        result = run_experiment(observations)
        self.assertEqual(result["accepted_quotes"], 1)
        self.assertEqual(result["rejected_quotes"], 3)
        self.assertIsNone(result["open_position"])

    def test_extreme_finite_quote_prices_are_rejected_without_arithmetic_failure(self):
        for bid, ask in (
            ("1e-40", "1e-40"),
            ("1e999999", "1e999999"),
            ("100", "1e999999"),
        ):
            with self.subTest(bid=bid, ask=ask):
                result = run_experiment([quote(0, bid, ask)] + entry_quotes(start=1))
                self.assertEqual(result["rejected_quotes"], 1)
                self.assertEqual(result["accepted_quotes"], 3)
                self.assertIsInstance(result["open_position"], dict)

    def test_supported_quote_price_boundaries_are_accepted(self):
        for price in ("1e-8", "1e9"):
            with self.subTest(price=price):
                result = run_experiment([quote(0, price, price)])
                self.assertEqual(result["accepted_quotes"], 1)
                self.assertEqual(result["rejected_quotes"], 0)

    def test_decimal_configuration_has_finite_operating_bounds(self):
        positive_fields = (
            "initial_cash", "max_spread_bps", "take_profit_bps",
            "stop_loss_bps", "daily_loss_limit",
        )
        for field in (*positive_fields, "fee_per_order", "slippage_bps"):
            for value in (D("1e9") + 1, D("1e999999")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        run_experiment([], Config(**{field: value}))
        for field in positive_fields:
            with self.subTest(field=field, boundary="below minimum"):
                with self.assertRaises(ValueError):
                    run_experiment([], Config(**{field: D("1e-40")}))
            for value in (D("1e-8"), D("1e9")):
                with self.subTest(field=field, boundary=value):
                    result = run_experiment([], Config(**{field: value}))
                    self.assertEqual(result["accepted_quotes"], 0)
        zero_costs = run_experiment([], Config(fee_per_order=D(0), slippage_bps=D(0)))
        self.assert_money_equal(zero_costs["ending_equity"], D("50"))

    def test_future_observation_cannot_advance_watch_clock_or_block_current_quotes(self):
        observations = [quote(1000, "200")] + entry_quotes()
        result = run_experiment(observations, as_of=BASE + timedelta(seconds=2))
        self.assertEqual(result["rejected_quotes"], 1)
        self.assertEqual(result["accepted_quotes"], 3)
        self.assertIsInstance(result["open_position"], dict)
        self.assertEqual(result["trades"], [])

    def test_proposals_expire_from_source_quote_time_without_extending_freshness(self):
        observations = [
            quote(0, "100", quote_seconds=-9),
            quote(1, "100.01", quote_seconds=-8),
            quote(2, "100.02", quote_seconds=-7),
            quote(3, "100.30", quote_seconds=-6),
        ]
        result = run_experiment(observations)
        self.assertEqual(len(result["proposals"]), 2)
        for proposal in result["proposals"]:
            observed = datetime.fromisoformat(proposal["at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(proposal["expires_at"].replace("Z", "+00:00"))
            self.assertEqual(expires - observed, timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
