"""TypeSafe Jev as the regime classifier.

Contract read from the live docs on 2026-09-18:
``POST {base}/v1/systemone`` with ``{state, model, questions}`` returns
``{answers: {id: {type, choice, probabilities, confidence}}}`` for a Choice.

What matters here is not the HTTP plumbing but the two promises the rest of the
system relies on: one call can classify the whole book, and a Jev answer can
only ever *block* an entry — never create one, and never crash the loop when the
service is down.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from agentic_trading.llm.jev import (
    JevClient,
    JevRegimeGate,
    build_jev_gate,
    build_questions,
    parse_regime_answers,
    symbol_state,
)
from agentic_trading.llm.market import features_from_closes
from agentic_trading.llm.regime import RegimeView


def _features(symbol: str = "SPY"):
    closes = [100.0 * (1.004**index) for index in range(60)]
    features = features_from_closes(symbol, closes)
    assert features is not None
    return features


def _choice(regime: str, confidence: float, **rest: float) -> dict[str, Any]:
    probabilities = {regime: confidence}
    probabilities.update(rest)
    return {
        "type": "choice",
        "choice": regime,
        "confidence": confidence,
        "probabilities": probabilities,
    }


class _FakeJev:
    """Records requests and returns canned answers."""

    model = "jev-latest"

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def system_one(self, *, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"state": state, "questions": questions})
        if self.error is not None:
            raise self.error
        return {"model": self.model, "answers": self.answers}


class QuestionTests(unittest.TestCase):
    def test_one_choice_question_per_symbol_with_named_meanings(self) -> None:
        questions = build_questions(["SPY", "BTC-USD"])
        self.assertEqual(sorted(questions), ["regime_BTC-USD", "regime_SPY"])
        spy = questions["regime_SPY"]
        self.assertEqual(spy["type"], "choice")
        # The id is never sent to the model, so the symbol must appear in the text.
        self.assertIn("SPY", spy["instructions"])
        self.assertEqual(
            sorted(spy["criteria"]), ["chop", "panic", "trend_down", "trend_up"]
        )

    def test_state_carries_the_numbers_and_the_symbol(self) -> None:
        state = symbol_state("SPY", _features())
        self.assertEqual(state["symbol"], "SPY")
        self.assertIn("trend_pct", state)
        self.assertIn("vol_pct", state)

    def test_missing_features_are_named_rather_than_invented(self) -> None:
        state = symbol_state("SPY", None)
        self.assertEqual(state, {"symbol": "SPY", "note": mock.ANY})


class ParseTests(unittest.TestCase):
    def test_a_choice_answer_becomes_a_gate_view(self) -> None:
        views = parse_regime_answers(
            {"answers": {"regime_SPY": _choice("trend_up", 0.82, chop=0.12)}},
            symbols=["SPY"],
            model="jev-latest",
        )
        view = views["SPY"]
        self.assertEqual(view.regime, "trend_up")
        self.assertAlmostEqual(view.confidence, 0.82)
        # The distribution is kept in the reason, so the console can show why.
        self.assertIn("trend_up 0.82", view.reason)
        self.assertIn("chop 0.12", view.reason)

    def test_one_bad_answer_does_not_lose_the_batch(self) -> None:
        views = parse_regime_answers(
            {
                "answers": {
                    "regime_SPY": _choice("chop", 0.7),
                    "regime_QQQ": {"type": "choice", "choice": "sideways"},
                    "regime_IWM": "not an object",
                }
            },
            symbols=["SPY", "QQQ", "IWM"],
        )
        self.assertEqual(list(views), ["SPY"])

    def test_a_garbled_response_yields_no_views(self) -> None:
        for payload in (None, {}, {"answers": None}, {"answers": []}):
            self.assertEqual(parse_regime_answers(payload, symbols=["SPY"]), {})

    def test_confidence_is_clamped(self) -> None:
        views = parse_regime_answers(
            {"answers": {"regime_SPY": _choice("chop", 1.7)}}, symbols=["SPY"]
        )
        self.assertLessEqual(views["SPY"].confidence, 1.0)


class ClientTests(unittest.TestCase):
    def test_it_posts_the_documented_contract(self) -> None:
        captured: dict[str, Any] = {}

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"answers": {"x": _choice("chop", 0.6)}}

        class _Client:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["kwargs"] = kwargs

            def __enter__(self) -> "_Client":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def post(self, url: str, *, json: Any, headers: dict) -> _Response:
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return _Response()

        client = JevClient(api_key="sk-test")
        with mock.patch("agentic_trading.llm.jev.httpx.Client", _Client):
            payload = client.system_one(state={"a": 1}, questions={"x": {"type": "noul"}})
        self.assertEqual(captured["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(captured["json"]["model"], "jev-latest")
        self.assertEqual(payload["answers"]["x"]["choice"], "chop")

    def test_a_custom_base_url_is_honoured(self) -> None:
        client = JevClient(api_key="k", base_url="https://example.test/")
        self.assertEqual(client.base_url, "https://example.test")


class GateTests(unittest.TestCase):
    def _gate(self, *, ttl: float = 900.0) -> tuple[JevRegimeGate, _FakeJev]:
        client = _FakeJev()
        gate = JevRegimeGate(client, ttl_seconds=ttl)  # type: ignore[arg-type]
        return gate, client

    def test_the_whole_book_is_classified_in_one_call(self) -> None:
        gate, client = self._gate()
        client.answers = {
            f"regime_{symbol}": _choice("trend_up", 0.8)
            for symbol in ("SPY", "QQQ", "IWM", "AAPL")
        }
        features = {"SPY": _features(), "QQQ": _features(), "IWM": _features(), "AAPL": _features()}
        views = gate.refresh_due(
            ["SPY", "QQQ", "IWM", "AAPL"], lambda symbol: features[symbol], max_per_pass=2
        )
        self.assertEqual(len(client.calls), 1, "one call for the whole book")
        self.assertEqual(len(views), 4)
        self.assertEqual({view.regime for view in views}, {"trend_up"})

    def test_chop_above_the_floor_blocks_and_below_it_does_not(self) -> None:
        gate, client = self._gate()
        client.answers = {"regime_SPY": _choice("chop", 0.65)}
        gate.refresh_many({"SPY": _features()})
        self.assertIsNotNone(gate.blocks("SPY"))

        other, other_client = self._gate()
        other_client.answers = {"regime_SPY": _choice("chop", 0.5)}
        other.refresh_many({"SPY": _features()})
        self.assertIsNone(other.blocks("SPY"))

    def test_a_trend_classification_never_blocks(self) -> None:
        for regime in ("trend_up", "trend_down"):
            gate, client = self._gate()
            client.answers = {"regime_SPY": _choice(regime, 0.95)}
            gate.refresh_many({"SPY": _features()})
            self.assertIsNone(gate.blocks("SPY"), regime)

    def test_a_service_outage_is_recorded_and_changes_nothing(self) -> None:
        gate, client = self._gate()
        client.error = RuntimeError("connection reset")
        views = gate.refresh_many({"SPY": _features()})
        self.assertEqual(views, {})
        self.assertEqual(gate.errors, 1)
        self.assertIn("connection reset", gate.last_error)
        # No view means no block: the gate can only ever remove trades.
        self.assertIsNone(gate.blocks("SPY"))

    def test_views_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "regimes.json"
            gate, client = self._gate()
            gate.state_path = path
            client.answers = {"regime_SPY": _choice("panic", 0.9)}
            gate.refresh_many({"SPY": _features()})
            restored = JevRegimeGate(_FakeJev(), state_path=path)  # type: ignore[arg-type]
        self.assertEqual(restored.view("SPY").regime, "panic")  # type: ignore[union-attr]

    def test_due_symbols_only(self) -> None:
        gate, client = self._gate(ttl=900.0)
        client.answers = {"regime_SPY": _choice("chop", 0.7)}
        gate.refresh_many({"SPY": _features()})
        client.calls.clear()
        views = gate.refresh_due(["SPY"], lambda symbol: _features(), max_per_pass=2)
        self.assertEqual(views, [], "a fresh view is not re-asked")
        self.assertEqual(client.calls, [])


class SelectionTests(unittest.TestCase):
    def test_no_key_means_no_jev_gate(self) -> None:
        with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}, clear=False):
            self.assertIsNone(build_jev_gate())

    def test_a_key_builds_the_gate_with_the_documented_defaults(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"TYPESAFE_API_KEY": "sk-live", "AGENTIC_SKIP_DOTENV": "1"},
            clear=False,
        ):
            gate = build_jev_gate()
        assert gate is not None
        self.assertEqual(gate.client.base_url, "https://api.typesafe.ai")
        self.assertEqual(gate.model, "jev-latest")

    def test_the_backend_switch_selects_jev(self) -> None:
        from agentic_trading.llm.advisor import build_regime_gate

        with mock.patch.dict(
            "os.environ",
            {
                "AGENTIC_LLM_ADVISOR": "1",
                "AGENTIC_REGIME_BACKEND": "jev",
                "TYPESAFE_API_KEY": "sk-live",
                "AGENTIC_SKIP_DOTENV": "1",
            },
            clear=False,
        ):
            gate = build_regime_gate()
        self.assertIsInstance(gate, JevRegimeGate)


if __name__ == "__main__":
    unittest.main()
