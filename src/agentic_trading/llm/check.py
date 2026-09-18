"""Smoke-test the configured model backends, without touching trading state.

Built because "I added the key" is not the same question as "the daemon can see
it and the service accepts it". This answers both: it reports which backends are
configured, makes one cheap live call to each, and prints latency and a sample
answer. Nothing here reads or writes the journal, the risk guard or the broker.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from agentic_trading.llm.client import build_llm_client, load_dotenv


def _key_state(name: str) -> str:
    return "set" if os.environ.get(name) else "NOT SET"


def check_chat_model() -> dict[str, Any]:
    """One minimal completion against the OpenAI-compatible endpoint."""
    from agentic_trading.llm.client import FakeLlmClient

    client = build_llm_client()
    if isinstance(client, FakeLlmClient):
        return {
            "name": "chat model (entry veto + regime)",
            "configured": False,
            "detail": f"AGENTIC_LLM_API_KEY is {_key_state('AGENTIC_LLM_API_KEY')}",
        }
    started = time.monotonic()
    try:
        reply = client.complete("You are a health check.", "Reply with OK.")
    except Exception as exc:  # noqa: BLE001 — a check reports, it does not raise
        return {
            "name": "chat model (entry veto + regime)",
            "configured": True,
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }
    return {
        "name": "chat model (entry veto + regime)",
        "configured": True,
        "ok": True,
        "model": getattr(client, "model", ""),
        "latency_seconds": round(time.monotonic() - started, 2),
        "sample": str(reply).strip()[:60],
    }


def check_jev() -> dict[str, Any]:
    """One real systemone call with two symbols, against the live endpoint."""
    from agentic_trading.llm.jev import API_KEY_ENV, build_jev_gate
    from agentic_trading.llm.market import features_from_closes

    if not os.environ.get(API_KEY_ENV):
        return {
            "name": "TypeSafe Jev (regime classifier)",
            "configured": False,
            "detail": f"{API_KEY_ENV} is {_key_state(API_KEY_ENV)}",
            "how_to_fix": (
                "add a line to .env: export TYPESAFE_API_KEY=... then "
                "export AGENTIC_REGIME_BACKEND=jev"
            ),
        }
    gate = build_jev_gate()
    if gate is None:  # pragma: no cover — build_jev_gate only returns None unfunded
        return {"name": "TypeSafe Jev (regime classifier)", "configured": True, "ok": False}
    closes = [100.0 * (1.003**index) for index in range(60)]
    features = features_from_closes("SPY", closes)
    payload_state = {"SPY": features, "BTC-USD": features}
    started = time.monotonic()
    try:
        views = gate.refresh_many(payload_state)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "TypeSafe Jev (regime classifier)",
            "configured": True,
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }
    return {
        "name": "TypeSafe Jev (regime classifier)",
        "configured": True,
        "ok": bool(views),
        "model": getattr(gate.client, "model", ""),
        "latency_seconds": round(time.monotonic() - started, 2),
        "views": {
            symbol: {"regime": view.regime, "confidence": round(view.confidence, 3)}
            for symbol, view in views.items()
        },
        "detail": "" if views else "the service answered but no usable choices came back",
    }


def run_checks(*, include_chat: bool = True) -> list[dict[str, Any]]:
    load_dotenv()
    results = [check_jev()]
    if include_chat:
        results.insert(0, check_chat_model())
    return results


def format_results(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for result in results:
        if not result.get("configured"):
            lines.append(f"NOT CONFIGURED  {result['name']}: {result.get('detail', '')}")
            if result.get("how_to_fix"):
                lines.append(f"                {result['how_to_fix']}")
            continue
        if result.get("ok"):
            detail = f"{result.get('model', '')} in {result.get('latency_seconds')}s"
            if result.get("sample"):
                detail += f" · said: {result['sample']!r}"
            if result.get("views"):
                detail += " · " + ", ".join(
                    f"{symbol} {view['regime']} {view['confidence']}"
                    for symbol, view in result["views"].items()
                )
            lines.append(f"OK              {result['name']}: {detail}")
        else:
            lines.append(f"FAILED          {result['name']}: {result.get('detail', '')}")
    return "\n".join(lines)
