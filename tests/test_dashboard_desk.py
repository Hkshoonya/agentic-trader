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


from agentic_trading.dashboard_desk import DeskEventCache, story, ticker_item  # noqa: E402


def _member(name, now_pct, *, samples=3, bench=False):
    return {"name": name, "label": label(name), "is_benchmark": bench,
            "now_pct": now_pct, "samples": samples}


ALL_BENCH = {"weights": {"momentum_rotation": 0.0, "benchmark": 1.0}}


class TickerTests(unittest.TestCase):
    def test_each_event_kind_reads_as_a_sentence(self) -> None:
        cases = [
            ({"event": "member_fill", "member": "momentum_rotation", "side": "buy",
              "symbol": "AAPL", "quantity": "0.04", "price": "250"},
             "fill", "Momentum rotation bought AAPL $10.00 on paper"),
            ({"event": "accepted", "side": "sell", "symbol": "QQQ", "notional": "29.7",
              "mode": "shadow", "intent": {"reason": "desk_rebalance"}},
             "order", "Desk sold QQQ $29.70 for the account, on paper"),
            ({"event": "desk_allocation", "changed": True,
              "allocations": {"momentum_rotation": 0.4, "benchmark": 0.6}},
             "allocation", "New weekly allocation: Buy-and-hold 60%, Momentum rotation 40%"),
            ({"event": "desk_allocation", "changed": False, "allocations": {"benchmark": 1.0}},
             "allocation", "Weekly allocation checked: no change"),
            ({"event": "advisory_overruled", "layer": "llm", "symbol": "SOL-USD"},
             "overruled", "The AI veto objected to SOL-USD; the desk followed its evidence"),
            ({"event": "desk_member_failed", "member": "trend_crypto", "error": "boom"},
             "error", "Crypto trend hit an error and sits out today"),
            ({"event": "selfcheck", "healthy": False, "failures": [{"name": "data"}]},
             "error", "Health check found a problem in data"),
            ({"event": "kill_switch", "reason": "daily loss"},
             "error", "Kill switch engaged: trading stopped"),
        ]
        for record, kind, text in cases:
            with self.subTest(event=record["event"]):
                item = ticker_item({**record, "at": "2026-09-29T21:00:00+00:00"})
                self.assertEqual((item["kind"], item["text"]), (kind, text))
                self.assertEqual(item["at"], "2026-09-29T21:00:00+00:00")

    def test_ordinary_orders_and_healthy_checks_are_not_ticker_news(self) -> None:
        self.assertIsNone(ticker_item({"event": "accepted", "intent": {"reason": "trend_entry"}}))
        self.assertIsNone(ticker_item({"event": "selfcheck", "healthy": True}))
        self.assertIsNone(ticker_item({"event": "cycle_stats"}))

    def test_the_view_lists_the_newest_thirty_first(self) -> None:
        events = [
            {"event": "member_fill", "member": "benchmark", "side": "buy", "symbol": "QQQ",
             "quantity": "1", "price": str(i), "at": f"2026-09-29T{i // 60:02d}:{i % 60:02d}:00+00:00"}
            for i in range(1, 41)
        ]
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            ticker = build_desk_view(config, events, now=NOW)["ticker"]
        self.assertEqual(len(ticker), 30)
        self.assertIn("$40.00", ticker[0]["text"])


class StoryTests(unittest.TestCase):
    def _story(self, members, allocation=ALL_BENCH, account=49.45, ticker=(), now=NOW):
        return story(members, allocation, account, list(ticker), symbols=19, now=now)

    def test_right_now_names_the_leader_against_buy_and_hold(self) -> None:
        said = self._story([_member("momentum_rotation", 1.16), _member("trend_crypto", -0.96),
                            _member("benchmark", -0.8, bench=True)])
        self.assertEqual(said["right_now"], "Momentum rotation is beating buy-and-hold by +1.96 points.")

    def test_right_now_when_nobody_is_ahead(self) -> None:
        said = self._story([_member("momentum_rotation", -1.0), _member("benchmark", 0.5, bench=True)])
        self.assertEqual(
            said["right_now"],
            "No strategy is ahead of buy-and-hold yet; the closest is Momentum rotation (-1.50 points).",
        )

    def test_right_now_before_any_prices(self) -> None:
        said = self._story([_member("momentum_rotation", None), _member("benchmark", None, bench=True)])
        self.assertEqual(said["right_now"], "The race starts when every book has its first prices.")

    def test_money_while_nobody_has_earned_capital(self) -> None:
        said = self._story([_member("momentum_rotation", 1.0, samples=4),
                            _member("benchmark", 0.0, bench=True)])
        self.assertEqual(
            said["money"],
            "All $49.45 sits in buy-and-hold (60% QQQ, 40% BTC). No strategy has earned "
            "capital yet: the furthest along has 4 of 20 daily samples.",
        )

    def test_money_when_samples_are_enough_but_the_edge_is_not(self) -> None:
        said = self._story([_member("momentum_rotation", 1.0, samples=25),
                            _member("benchmark", 0.0, bench=True)])
        self.assertTrue(said["money"].endswith(
            "No strategy has earned capital yet: none has beaten buy-and-hold convincingly."))

    def test_money_when_capital_follows_a_winner(self) -> None:
        said = self._story(
            [_member("momentum_rotation", 3.0, samples=25), _member("benchmark", 0.0, bench=True)],
            allocation={"weights": {"momentum_rotation": 0.4, "benchmark": 0.6}},
        )
        self.assertEqual(said["money"],
                         "Capital follows the evidence: Momentum rotation 40%, buy-and-hold 60%.")

    def test_money_before_the_first_allocation(self) -> None:
        said = self._story([_member("benchmark", 0.0, bench=True)], allocation={"weights": {}})
        self.assertEqual(said["money"], "The desk has not made its first allocation yet.")

    def test_just_now_is_the_latest_news_or_the_quiet_watch(self) -> None:
        fresh = [{"at": "2026-09-29T21:30:00+00:00", "kind": "fill", "text": "Crypto trend bought SPY $9.44 on paper"}]
        stale = [{"at": "2026-09-28T01:00:00+00:00", "kind": "fill", "text": "old news"}]
        members = [_member("benchmark", 0.0, bench=True)]
        self.assertEqual(self._story(members, ticker=fresh)["just_now"],
                         "Crypto trend bought SPY $9.44 on paper.")
        self.assertEqual(self._story(members, ticker=stale)["just_now"],
                         "Watching 19 symbols; nothing needs doing right now.")

    def test_the_view_carries_the_story(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            said = build_desk_view(config, [], now=NOW)["story"]
        self.assertEqual(said["right_now"], "Momentum rotation is beating buy-and-hold by +1.50 points.")
        self.assertIn("All $50.10 sits in buy-and-hold", said["money"])

    def test_a_non_desk_bot_still_gets_a_story(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(Path(name), strategy="trend_crypto")
            view = build_desk_view(config, [], now=NOW)
        self.assertFalse(view["enabled"])
        self.assertEqual(view["story"]["right_now"],
                         "The cockpit follows the strategy desk; this bot runs Crypto trend on its own.")
        self.assertEqual(view["story"]["money"], "Its orders and positions are on the Orders tab.")


class EventCacheTests(unittest.TestCase):
    def _journal(self, tmp: Path) -> Path:
        journal = tmp / "journal"
        journal.mkdir()
        lines = [
            json.dumps({"event": "member_fill", "member": "benchmark", "side": "buy",
                        "symbol": "QQQ", "quantity": "1", "price": "1", "at": "2026-09-28T01:00:00+00:00"}),
            json.dumps({"event": "stale_quotes_rejected", "at": "2026-09-28T01:00:01+00:00"}),
            '{"event": "member_fill", "memb',             # cut by a power loss
            "\x00\x00\x00",                               # a null-byte tail
            json.dumps({"event": "accepted", "intent": {"reason": "trend_entry"}, "at": "x"}),
            json.dumps({"event": "accepted", "intent": {"reason": "desk_rebalance"},
                        "side": "buy", "symbol": "QQQ", "notional": "1", "at": "2026-09-28T02:00:00+00:00"}),
        ]
        (journal / "2026-09-28.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (journal / "archive-2026-09-16-tif.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
        return journal

    def test_it_keeps_only_desk_news_and_skips_broken_lines(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            events = DeskEventCache(self._journal(Path(name))).read()
        self.assertEqual([e["event"] for e in events], ["member_fill", "accepted"])

    def test_an_unchanged_file_is_not_re_read(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            journal = self._journal(Path(name))
            cache = DeskEventCache(journal)
            first = cache.read()
            from unittest import mock

            with mock.patch("agentic_trading.dashboard_desk._relevant_events") as parse:
                self.assertEqual(cache.read(), first)
            parse.assert_not_called()

    def test_a_missing_journal_dir_is_empty(self) -> None:
        self.assertEqual(DeskEventCache(Path("/nonexistent/journal")).read(), [])


class DeskEndpointTests(unittest.TestCase):
    def _get(self, config, path: str) -> dict:
        import threading
        import urllib.request

        from agentic_trading.dashboard import serve

        server = serve(config, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}{path}"
            with urllib.request.urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                return json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()

    def test_the_cockpit_endpoint_serves_the_desk_view(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            journal = Path(config.journal_dir)
            journal.mkdir(parents=True, exist_ok=True)
            (journal / "2026-09-29.jsonl").write_text(json.dumps({
                "event": "member_fill", "member": "momentum_rotation", "side": "buy",
                "symbol": "AAPL", "quantity": "0.04", "price": "250",
                "at": "2026-09-29T21:00:00+00:00"}) + "\n", encoding="utf-8")
            payload = self._get(config, "/api/desk")
        self.assertTrue(payload["enabled"])
        self.assertEqual([m["name"] for m in payload["members"]], ["momentum_rotation", "benchmark"])
        self.assertEqual(payload["ticker"][0]["text"], "Momentum rotation bought AAPL $10.00 on paper")

    def test_a_failure_inside_the_view_is_reported_not_raised(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as name:
            config = _config(_desk(Path(name)))
            with mock.patch("agentic_trading.dashboard.build_desk_view",
                            side_effect=RuntimeError("boom")):
                payload = self._get(config, "/api/desk")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["error"], "RuntimeError: boom")
        self.assertEqual(payload["members"], [])


if __name__ == "__main__":
    unittest.main()
