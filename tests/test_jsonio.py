"""State files and the dashboard wire must be strict JSON."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from agentic_trading import jsonio


class StrictJsonTests(unittest.TestCase):
    def test_non_finite_floats_become_null(self) -> None:
        payload = jsonio.finite(
            {
                "inf": float("inf"),
                "neg": float("-inf"),
                "nan": float("nan"),
                "ok": 1.5,
                "nested": [float("inf"), {"deep": float("nan")}],
            }
        )
        self.assertIsNone(payload["inf"])
        self.assertIsNone(payload["neg"])
        self.assertIsNone(payload["nan"])
        self.assertEqual(payload["ok"], 1.5)
        self.assertEqual(payload["nested"], [None, {"deep": None}])

    def test_dumps_never_emits_bare_infinity(self) -> None:
        text = jsonio.dumps(
            {"profit_factor": float("inf"), "equity": Decimal("50.00")}
        )
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)
        decoded = json.loads(text)
        self.assertIsNone(decoded["profit_factor"])
        self.assertEqual(decoded["equity"], "50.00")

    def test_finite_leaves_ordinary_values_alone(self) -> None:
        value = {"a": [1, "two", None, True], "b": {"c": Decimal("1.25")}}
        self.assertEqual(jsonio.finite(value), value)


if __name__ == "__main__":
    unittest.main()
