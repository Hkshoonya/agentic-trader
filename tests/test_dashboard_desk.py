"""/api/desk: the strategy desk as the cockpit shows it, read-only."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from agentic_trading.config import load_config
from agentic_trading.dashboard_desk import build_desk_view, label, next_allocation
from agentic_trading.desk.book import MemberBook

NOW = datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc)


def _config(tmp: Path, members: str = '["momentum_rotation", "benchmark"]', strategy: str = "desk"):
    from tests.test_runtime_daemon import _write_config

    bars = tmp / "bars"
    bars.mkdir(exist_ok=True)
    return load_config(
        _write_config(
            tmp,
            extra=[
                f'history_path = "{bars}"',
                f'strategy = "{strategy}"',
                f"desk_members = {members}",
            ],
        )
    )


def _book(desk: Path, name: str, *, samples, cash="0", positions=None, prices=None,
          entries=1, start="50", cash_pending=False) -> None:
    book = MemberBook(name, starting_equity=Decimal(start), path=desk / f"{name}.json")
    book.cash = Decimal(cash)
    book.positions = {s: Decimal(q) for s, q in (positions or {}).items()}
    book.prices = {s: Decimal(p) for s, p in (prices or {}).items()}
    book.samples = [(d, str(e)) for d, e in samples]
    book.entries = entries
    book.cash_pending = cash_pending
    book.save()


def _desk(tmp: Path) -> Path:
    config = _config(tmp)
    desk = Path(config.state_dir) / "desk"
    _book(desk, "momentum_rotation",
          samples=[("2026-09-25", "50.5"), ("2026-09-26", "51")],
          cash="1", positions={"AAPL": "0.2"}, prices={"AAPL": "250"}, entries=3)
    _book(desk, "benchmark",
          samples=[("2026-09-25", "50"), ("2026-09-26", "50.25")], cash="50.25")
    _book(desk, "account", samples=[("2026-09-25", "50"), ("2026-09-26", "50.1")],
          cash="50.1", entries=0)
    (desk / "desk.json").write_text(json.dumps({
        "allocations": {"momentum_rotation": 0.0, "benchmark": 1.0},
        "allocation_week": "2026-09-28",
    }))
    return tmp


class DeskViewTests(unittest.TestCase):
    def test_each_member_races_from_its_own_start(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            view = build_desk_view(config, [], now=NOW)
        self.assertTrue(view["enabled"])
        rot, bench = view["members"]
        self.assertEqual(rot["label"], "Momentum rotation")
        self.assertEqual(rot["series"], [["2026-09-25", 1.0], ["2026-09-26", 2.0]])
        self.assertEqual(rot["now_pct"], 2.0)
        self.assertEqual(rot["value"], 51.0)
        self.assertEqual(rot["holdings"], [{"symbol": "AAPL", "value": 50.0}])
        self.assertEqual(rot["entries"], 3)
        self.assertTrue(bench["is_benchmark"])
        self.assertEqual(bench["series"], [["2026-09-25", 0.0], ["2026-09-26", 0.5]])
        self.assertEqual(bench["now_pct"], 0.5)

    def test_qualification_comes_from_the_allocator_itself(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            rot = build_desk_view(config, [], now=NOW)["members"][0]
        self.assertEqual(rot["samples"], 2)
        self.assertEqual(rot["samples_needed"], 20)
        self.assertIsNone(rot["t"])
        self.assertEqual(rot["t_needed"], 1.0)
        self.assertEqual(rot["reason"], "2 daily samples (needs 20)")
        self.assertEqual(rot["weight"], 0.0)

    def test_allocation_carries_weights_legs_history_and_next_monday(self) -> None:
        events = [
            {"event": "desk_allocation", "week": "2026-09-21",
             "allocations": {"benchmark": 1.0}, "changed": False},
            {"event": "desk_allocation", "week": "2026-09-28",
             "allocations": {"benchmark": 1.0}, "changed": False},
        ]
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            allocation = build_desk_view(config, events, now=NOW)["allocation"]
        self.assertEqual(allocation["week"], "2026-09-28")
        self.assertEqual(allocation["weights"], {"momentum_rotation": 0.0, "benchmark": 1.0})
        self.assertEqual(allocation["legs"], {"QQQ": 0.6, "BTC-USD": 0.4})
        self.assertEqual([h["week"] for h in allocation["history"]], ["2026-09-21", "2026-09-28"])
        self.assertEqual(allocation["next_at"], "2026-10-05T00:00:00+00:00")

    def test_the_account_book_gives_the_account_tile(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            account = build_desk_view(config, [], now=NOW)["account"]
        self.assertEqual(account["value"], 50.1)
        self.assertEqual(account["series"], [["2026-09-25", 50.0], ["2026-09-26", 50.1]])

    def test_a_member_without_a_book_yet_is_shown_empty(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp, members='["momentum_rotation", "dip_reversal", "benchmark"]')
            view = build_desk_view(config, [], now=NOW)
        dip = next(m for m in view["members"] if m["name"] == "dip_reversal")
        self.assertEqual(dip["series"], [])
        self.assertIsNone(dip["now_pct"])
        self.assertEqual(dip["reason"], "no paper book yet")
        self.assertEqual(dip["t_needed"], 1.0)  # two non-benchmark members: unchanged bar

    def test_unparseable_samples_are_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp)
            _book(Path(config.state_dir) / "desk", "momentum_rotation",
                  samples=[("2026-09-25", "NaN"), ("2026-09-26", ""), ("2026-09-27", "51")],
                  cash="51")
            rot = build_desk_view(config, [], now=NOW)["members"][0]
        self.assertEqual(rot["series"], [["2026-09-27", 2.0]])

    def test_a_book_whose_cash_is_pending_has_no_live_point(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp)
            _book(Path(config.state_dir) / "desk", "momentum_rotation",
                  samples=[("2026-09-25", "50.5")], cash="0", cash_pending=True)
            rot = build_desk_view(config, [], now=NOW)["members"][0]
        self.assertIsNone(rot["now_pct"])
        self.assertIsNone(rot["value"])

    def test_corrupt_desk_state_degrades_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            tmp = _desk(Path(name))
            config = _config(tmp)
            desk = Path(config.state_dir) / "desk"
            (desk / "desk.json").write_text("{not json")
            (desk / "momentum_rotation.json").write_text("\x00\x00")
            view = build_desk_view(config, [], now=NOW)
        self.assertEqual(view["allocation"]["weights"], {})
        self.assertEqual(view["members"][0]["reason"], "no paper book yet")

    def test_no_trial_means_a_null_trial(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            self.assertIsNone(build_desk_view(config, [], now=NOW)["trial"])

    def test_labels_and_the_next_monday(self) -> None:
        self.assertEqual(label("trend_crypto"), "Crypto trend")
        self.assertEqual(label("dip_reversal"), "Dip buyer")
        self.assertEqual(label("benchmark"), "Buy-and-hold")
        self.assertEqual(label("some_new_rule"), "Some new rule")
        monday = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(next_allocation(monday), datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(next_allocation(NOW), monday)


if __name__ == "__main__":
    unittest.main()
