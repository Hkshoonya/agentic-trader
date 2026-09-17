"""State files and the dashboard wire must be strict JSON."""

from __future__ import annotations

import json
import threading
import unittest
from decimal import Decimal
from pathlib import Path
import tempfile

from agentic_trading import jsonio
from agentic_trading.limits import Limits, load_limits, save_limits


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


class AtomicWriteTests(unittest.TestCase):
    def test_write_text_leaves_no_temporary_files_behind(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            target = Path(name) / "state.json"
            jsonio.write_text(target, jsonio.dumps({"ok": True}) + "\n")
            self.assertEqual(json.loads(target.read_text())["ok"], True)
            self.assertEqual(sorted(p.name for p in Path(name).iterdir()), ["state.json"])

    def test_a_reader_never_sees_a_half_written_limits_file(self) -> None:
        """A torn read used to mean "no limits", i.e. back to the full ceiling."""
        with tempfile.TemporaryDirectory() as name:
            state_dir = Path(name)
            save_limits(
                state_dir, Limits("0.02", "0.08", "test", "now", confidence="0.5")
            )
            stop = threading.Event()
            failures: list[str] = []

            def reader() -> None:
                while not stop.is_set():
                    loaded = load_limits(state_dir)
                    if loaded is None:
                        failures.append("torn read")
                        return

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            try:
                for i in range(300):
                    save_limits(
                        state_dir,
                        Limits(
                            "0.02",
                            "0.08",
                            f"rewrite-{i}",
                            "now",
                            confidence="0.5",
                        ),
                    )
            finally:
                stop.set()
                thread.join(timeout=5)

            self.assertEqual(failures, [])
            self.assertIsNotNone(load_limits(state_dir))


if __name__ == "__main__":
    unittest.main()
