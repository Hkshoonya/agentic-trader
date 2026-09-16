"""Order requests must satisfy Robinhood's documented parameter rules."""

from __future__ import annotations

import unittest
from decimal import Decimal

from agentic_trading.orders import EquityOrderRequest, OrderValidationError
from agentic_trading.types import Side


def request(**overrides) -> EquityOrderRequest:
    base = {
        "account_number": "A1",
        "symbol": "spy",
        "side": Side.BUY,
        "order_type": "market",
        "dollar_amount": Decimal("25.00"),
        "market_hours": "regular_hours",
    }
    base.update(overrides)
    return EquityOrderRequest(**base)  # type: ignore[arg-type]


class ValidationTests(unittest.TestCase):
    def test_dollar_market_buy_is_valid_and_normalises_symbol(self) -> None:
        req = request()
        self.assertEqual(req.symbol, "SPY")
        self.assertEqual(
            req.to_mcp_args(),
            {
                "account_number": "A1",
                "symbol": "SPY",
                "side": "buy",
                "type": "market",
                "market_hours": "regular_hours",
                "time_in_force": "gfd",
                "dollar_amount": "25.00",
            },
        )

    def test_requires_exactly_one_size_field(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(dollar_amount=None)
        with self.assertRaises(OrderValidationError):
            request(quantity=Decimal("1"))

    def test_dollar_amount_requires_regular_hours_market(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(market_hours="extended_hours")
        with self.assertRaises(OrderValidationError):
            request(order_type="limit", limit_price=Decimal("10"))

    def test_limit_requires_limit_price_and_forbids_it_elsewhere(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(order_type="limit", dollar_amount=None, quantity=Decimal("1"))
        with self.assertRaises(OrderValidationError):
            request(limit_price=Decimal("10"))

    def test_market_orders_must_be_gfd(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(time_in_force="gtc")

    def test_extended_hours_only_allows_limit_orders(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(
                order_type="market",
                dollar_amount=None,
                quantity=Decimal("2"),
                market_hours="extended_hours",
            )
        req = request(
            order_type="limit",
            dollar_amount=None,
            quantity=Decimal("2"),
            limit_price=Decimal("100.50"),
            market_hours="extended_hours",
        )
        self.assertEqual(req.market_hours, "extended_hours")

    def test_fractional_requires_regular_hours_market(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(
                order_type="limit",
                dollar_amount=None,
                quantity=Decimal("0.5"),
                limit_price=Decimal("100"),
            )
        req = request(
            dollar_amount=None,
            quantity=Decimal("0.123456"),
        )
        self.assertEqual(req.to_mcp_args()["quantity"], "0.123456")

    def test_fractional_limited_to_six_places(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(dollar_amount=None, quantity=Decimal("0.1234567"))

    def test_stop_orders_require_stop_price(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(
                order_type="stop_market",
                dollar_amount=None,
                quantity=Decimal("1"),
            )
        req = request(
            order_type="stop_market",
            dollar_amount=None,
            quantity=Decimal("1"),
            stop_price=Decimal("95.00"),
        )
        self.assertEqual(req.to_mcp_args()["stop_price"], "95.00")

    def test_sell_uses_quantity_and_omits_unset_fields(self) -> None:
        req = request(
            side=Side.SELL,
            dollar_amount=None,
            quantity=Decimal("0.5"),
        )
        args = req.to_mcp_args()
        self.assertEqual(args["side"], "sell")
        self.assertNotIn("dollar_amount", args)
        self.assertNotIn("limit_price", args)
        self.assertNotIn("ref_id", args)

    def test_notional_bound_prefers_dollar_amount(self) -> None:
        self.assertEqual(request().notional_bound, Decimal("25.00"))
        limit_req = request(
            order_type="limit",
            dollar_amount=None,
            quantity=Decimal("2"),
            limit_price=Decimal("100"),
        )
        self.assertEqual(limit_req.notional_bound, Decimal("200"))

    def test_rejects_non_positive_amounts(self) -> None:
        with self.assertRaises(OrderValidationError):
            request(dollar_amount=Decimal("0"))
        with self.assertRaises(OrderValidationError):
            request(
                order_type="limit",
                dollar_amount=None,
                quantity=Decimal("1"),
                limit_price=Decimal("-1"),
            )


if __name__ == "__main__":
    unittest.main()
