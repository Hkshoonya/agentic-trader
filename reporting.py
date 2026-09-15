"""Offline, dependency-free exports for a quote-driven paper experiment.

Reports describe simulated fills only. Nothing in this module accesses a
brokerage, starts a server, or transmits data.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from html import escape
import io
import json
import os
from pathlib import Path
import tempfile


TRADE_FIELDS = (
    "entry_at", "exit_at", "quantity", "entry_price", "exit_price",
    "entry_cost", "exit_proceeds", "entry_fee", "exit_fee", "net_pnl",
    "exit_reason", "hold_seconds",
)
PROPOSAL_FIELDS = (
    "id", "at", "action", "symbol", "quantity", "reference_bid",
    "reference_ask", "modeled_price", "reason", "expires_at", "mode",
)


def _plain(value: object) -> str:
    if value is None:
        return "Not available"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _number(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _money(value: object, signed: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "Not available"
    prefix = "−" if number < 0 else "+" if signed and number > 0 else ""
    # Sub-cent outcomes matter for a $50 experiment. Exports retain precision.
    return f"{prefix}${abs(number):,.6f}"


def _md(value: object) -> str:
    return escape(_plain(value)).replace("|", "&#124;").replace("\n", "<br>")


def _csv_cell(value: object) -> str:
    text = "" if value is None else _plain(value)
    # Prevent external symbol/reason text from becoming spreadsheet formulas.
    # Finite numeric values (including negative P&L) stay machine-readable.
    if text.lstrip().startswith(("=", "+", "-", "@")) and _number(text) is None:
        return "'" + text
    if text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _csv(rows: list[dict], fields: tuple[str, ...]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_cell(row.get(field)) for field in fields})
    return stream.getvalue()


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _source(result: dict) -> str:
    source = result.get("data_source")
    names = {
        "recorded_live_quotes": "Recorded Robinhood quotes; simulated replay (source label from input)",
        "quote_file": "Quote-file replay (source labels from input)",
        "synthetic_or_unspecified": "Synthetic or unspecified quote source; simulated replay",
    }
    if source is None:
        return "Quote-file replay; source provenance not supplied"
    return names.get(str(source), _plain(source))


def _status(result: dict) -> str:
    value = result.get("status")
    return {
        "open_position_at_end": "Open paper position",
        "flat_at_end": "No open position",
        "loss_limit_reached": "Loss threshold reached",
        "trade_limit_reached": "Trade limit reached",
        "no_valid_quotes": "Waiting for valid quotes",
    }.get(str(value), _plain(value))


def _summary_rows(result: dict) -> list[tuple[str, str]]:
    return [
        ("Status", _status(result)),
        ("Symbol", _plain(result.get("symbol"))),
        ("Data source", _source(result)),
        ("Run mode", _plain(result.get("run_mode", "replay"))),
        ("Report generated at", _plain(result.get("report_generated_at"))),
        ("Quote feed status at report generation", _plain(result.get("feed_status", "replay"))),
        ("Quote feed age in seconds", _plain(result.get("quote_feed_age_seconds"))),
        ("First accepted observation", _plain(result.get("first_observed_at"))),
        ("Last accepted observation", _plain(result.get("last_observed_at"))),
        ("Last accepted source quote timestamp", _plain(result.get("last_quote_at"))),
        ("Starting simulated cash", _money(result.get("starting_cash"))),
        ("Available simulated cash", _money(result.get("cash"))),
        ("Ending simulated equity", _money(result.get("ending_equity"))),
        ("Realized P&L", _money(result.get("realized_pnl"), signed=True)),
        ("Total P&L, including open position marks", _money(result.get("total_pnl"), signed=True)),
        ("Maximum observed drawdown", _money(result.get("max_drawdown"))),
        ("Cash-only benchmark equity", _money(result.get("cash_benchmark_equity"))),
        ("Completed simulated trades", str(len(result.get("trades", [])))),
        ("Accepted quotes", _plain(result.get("accepted_quotes", 0))),
        ("Rejected quotes", _plain(result.get("rejected_quotes", 0))),
    ]


def _markdown(result: dict) -> str:
    lines = [
        "# Paper trading experiment", "",
        "**Simulation only. No real orders were placed by this program.**", "",
        "Quoted market observations are replayed through a fixed strategy. Fills, "
        "positions, and P&L are simulated. These results do not establish a profitable edge.", "",
        "## Results", "", "| Measure | Result |", "| --- | --- |",
    ]
    lines += [f"| {_md(label)} | {_md(value)} |" for label, value in _summary_rows(result)]
    lines += ["", "## Open simulated position", ""]
    position = result.get("open_position")
    if position is None:
        lines += ["No open simulated position."]
    else:
        lines += [
            "**Still open.** No closing fill is invented at the end of the recording. "
            "Ending equity includes the engine's latest accepted quote mark; it is not a realized balance.",
            "", "| Field | Value |", "| --- | --- |",
        ]
        lines += [f"| {_md(key)} | {_md(value)} |" for key, value in position.items()]
    lines += ["", "## Completed simulated trades", ""]
    trades = result.get("trades", [])
    if trades:
        lines += ["| Entry | Exit | Quantity | Net P&L | Reason | Hold (seconds) |", "| --- | --- | --- | --- | --- | --- |"]
        for trade in trades:
            values = [trade.get("entry_at"), trade.get("exit_at"), trade.get("quantity"),
                      _money(trade.get("net_pnl"), signed=True), trade.get("exit_reason"), trade.get("hold_seconds")]
            lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    else:
        lines += ["No completed simulated trades. This does not establish whether the strategy is profitable."]
    lines += [
        "", "## Trade proposals", "",
        f"{len(result.get('proposals', []))} historical paper proposals are exported in `proposals.csv`.", "",
        "Proposals describe decisions at their recorded timestamps and have explicit expirations. "
        "They are not current executable instructions. Any manual trade requires fresh data and your own review.",
        "", "## Modeling assumptions and limitations", "",
    ]
    assumptions = result.get("assumptions", [])
    lines += [f"- {_md(item)}" for item in assumptions] or ["- No engine assumptions supplied."]
    lines += [
        "- Equity and drawdown reflect accepted observations only; unobserved market moves are not measured.",
        "- Dollar summaries display six decimal places; JSON and CSV retain engine precision.",
        "", "## Strategy configuration", "", "| Setting | Value |", "| --- | --- |",
    ]
    lines += [f"| {_md(key)} | {_md(value)} |" for key, value in result.get("config", {}).items()]
    lines += [
        "", "## Files", "",
        "- `result.json`: full engine result, including equity observations and assumptions.",
        "- `trades.csv`: completed simulated trades with modeled prices, costs, proceeds, and fees.",
        "- `proposals.csv`: historical decisions for paper/manual review only.",
        "- `events.jsonl`: recorded decisions and data-quality events.",
        "- `dashboard.html`: standalone, scriptless dashboard; open locally in a browser.", "",
    ]
    return "\n".join(lines)


def _chart(result: dict) -> str:
    points = []
    for item in result.get("equity_curve", []):
        equity = _number(item.get("equity"))
        if equity is not None:
            points.append(equity)
    if not points:
        return '<p class="empty">No accepted observations to plot.</p>'
    floor, ceiling = min(points), max(points)
    span = ceiling - floor
    if span == 0:
        span = max(abs(floor) * Decimal("0.0001"), Decimal("0.0001"))
        floor -= span / 2
        ceiling += span / 2
    # Observation index is intentional: sampling can be irregular.
    coordinates = " ".join(
        f"{50 + index * 850 / max(len(points) - 1, 1):.2f},"
        f"{30 + float((ceiling - value) / span) * 150:.2f}"
        for index, value in enumerate(points)
    )
    return (
        '<svg viewBox="0 0 950 225" role="img" aria-labelledby="chart-title chart-description">'
        '<title id="chart-title">Simulated equity by accepted observation</title>'
        '<desc id="chart-description">Each point is one accepted observation, equally spaced. '
        'The vertical range is scaled to show small changes. This is not a prediction.</desc>'
        '<path d="M50 30H900M50 105H900M50 180H900" stroke="#29403f" stroke-width="1"/>'
        f'<polyline points="{coordinates}" fill="none" stroke="#8ce3c6" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        + (f'<circle cx="50" cy="105" r="4" fill="#8ce3c6"/>' if len(points) == 1 else "")
        +
        f'<text x="50" y="20" fill="#a9bfba" font-size="13">{escape(_money(ceiling))}</text>'
        f'<text x="50" y="202" fill="#a9bfba" font-size="13">{escape(_money(floor))}</text>'
        f'<text x="900" y="220" text-anchor="end" fill="#a9bfba" font-size="13">'
        f'{len(points)} accepted observation marks</text></svg>'
    )


def _html_table(headers: list[str], rows: list[list[object]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    head = "".join(f"<th scope=\"col\">{escape(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(_plain(value))}</td>" for value in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _dashboard(result: dict) -> str:
    pnl = _number(result.get("total_pnl"))
    pnl_class = "negative" if pnl is not None and pnl < 0 else "positive" if pnl is not None and pnl > 0 else ""
    starting_cash = _number(result.get("starting_cash"))
    title = f"The ${starting_cash:,.2f} experiment." if starting_cash is not None else "The paper experiment."
    cards = [
        ("Ending simulated equity", _money(result.get("ending_equity")), "", "Includes any open position mark"),
        ("Total simulated P&L", _money(result.get("total_pnl"), signed=True), pnl_class, "After modeled execution costs"),
        ("Available simulated cash", _money(result.get("cash")), "", "Uncommitted paper balance"),
        ("Completed trades", str(len(result.get("trades", []))), "", "Simulated round trips"),
    ]
    card_html = "".join(f'<article class="metric"><p>{escape(label)}</p><strong class="{css}">{escape(value)}</strong><small>{escape(note)}</small></article>' for label, value, css, note in cards)
    trade_rows = [[item.get("entry_at"), item.get("exit_at"), item.get("quantity"),
                   _money(item.get("net_pnl"), signed=True), item.get("exit_reason"), item.get("hold_seconds")]
                  for item in result.get("trades", [])]
    proposal_rows = [[item.get("at"), item.get("action"), item.get("symbol"), item.get("quantity"),
                     _money(item.get("modeled_price")), item.get("expires_at"), item.get("reason")]
                    for item in result.get("proposals", [])]
    position = result.get("open_position")
    position_html = '<p class="empty">No open simulated position.</p>' if position is None else (
        '<p class="notice">Position remains open. No closing fill was invented. Its latest accepted '
        'quote mark contributes to ending equity and may be stale.</p>'
        + _html_table(["Field", "Value"], [[key, value] for key, value in position.items()], ""))
    assumption_html = "".join(f"<li>{escape(_plain(item))}</li>" for item in result.get("assumptions", [])) or "<li>No engine assumptions supplied.</li>"
    summary = _html_table(["Measure", "Recorded result"], [[label, value] for label, value in _summary_rows(result)], "No results.")
    config = _html_table(["Setting", "Value"], [[key, value] for key, value in result.get("config", {}).items()], "No strategy configuration supplied.")
    trades = _html_table(["Entry", "Exit", "Quantity", "Net P&L", "Exit reason", "Hold (s)"], trade_rows, "No completed trades in this recording. An empty trade log does not establish a profitable strategy.")
    proposals = _html_table(["Observed at", "Action", "Symbol", "Quantity", "Modeled price", "Expires at", "Reason"], proposal_rows, "No proposals generated from this recording.")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>Paper experiment · {escape(_plain(result.get("symbol")))}</title>
<style>
:root{{color-scheme:dark;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#101a19;color:#ecf3f0}}
*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1240px;margin:auto;padding:48px 28px 56px}}header{{margin-bottom:30px}}
.eyebrow{{color:#8ce3c6;letter-spacing:.14em;text-transform:uppercase;font-size:.73rem;font-weight:700}}h1{{font-family:Georgia,serif;font-weight:400;font-size:clamp(2.3rem,6vw,4rem);line-height:1.1;margin:15px 0}}h2{{font-size:1.15rem;font-weight:600;margin:0 0 18px}}
.subtitle,.empty,.caption{{color:#a9bfba;line-height:1.6}}.subtitle{{max-width:790px;font-size:1rem}}.badge{{display:inline-block;border:1px solid #5c766e;border-radius:100px;padding:7px 12px;color:#b6cebf;font-size:.75rem}}
.notice{{background:#242c23;color:#e0dab1;border-left:3px solid #c7bb75;padding:14px 17px;line-height:1.55;font-size:.9rem}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:28px 0}}.metric,section{{background:#172423;border:1px solid #2b3e3a;border-radius:12px;padding:22px}}.metric p{{margin:0 0 14px;color:#b6c9c2;font-size:.8rem}}.metric strong{{display:block;font-size:clamp(1.3rem,2.4vw,2rem);font-weight:550;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}.metric small{{display:block;color:#91a69e;font-size:.75rem;margin-top:13px}}
.positive{{color:#8ce3c6}}.negative{{color:#f4b2a4}}section{{margin-bottom:20px}}svg{{display:block;width:100%;height:auto}}.caption{{font-size:.8rem;margin:10px 0 0}}.columns{{display:grid;grid-template-columns:1.2fr 1fr;gap:20px}}.columns section{{min-width:0}}
.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:.8rem;text-align:left}}th{{color:#b9ccc4;font-size:.74rem;font-weight:500;padding:11px 12px;border-bottom:1px solid #446057;white-space:nowrap}}td{{padding:12px;border-bottom:1px solid #2b3e3a;vertical-align:top;line-height:1.5;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}}tr:last-child td{{border-bottom:0}}li{{color:#c0d0c9;line-height:1.65;margin:9px 0;font-size:.88rem}}ul{{padding-left:20px}}footer{{font-size:.78rem;color:#a9bfba;line-height:1.7}}a{{color:#8ce3c6}}code{{color:#d5e5dc}}a:focus-visible{{outline:2px solid #8ce3c6;outline-offset:4px}}
@media(max-width:900px){{.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.columns{{grid-template-columns:1fr;gap:0}}}}
@media(max-width:520px){{main{{padding:28px 14px}}.metric,section{{padding:16px}}.metrics{{gap:10px}}.metric strong{{font-size:1.2rem}}h1{{font-size:2.6rem}}th,td{{padding:10px 8px}}}}
@media print{{:root{{color-scheme:light;background:white;color:black}}.metric,section{{background:white;color:black;border-color:#ccc}}main{{padding:0}}.subtitle,.caption,td,th,li,footer{{color:#333}}.table-wrap{{overflow:visible}}section{{break-inside:avoid}}}}
</style></head><body><main>
<header><div class="eyebrow">Local research / paper simulation</div><h1>{escape(title)}</h1>
<p class="subtitle">Recorded quotes. Explicit execution assumptions. A traceable account of every simulated decision.</p>
<span class="badge">{escape(_plain(result.get("symbol")))} · {escape(_status(result))}</span></header>
<p class="notice"><strong>Simulation only.</strong> This program places no real orders. Market quotes may be recorded live; all fills and results shown here are simulated. These results do not establish a profitable edge.</p>
<div class="metrics">{card_html}</div>
<section><h2>Simulated equity</h2>{_chart(result)}<p class="caption">Each point is an accepted quote observation, equally spaced. The vertical scale magnifies small changes. Values between observations are unknown.</p></section>
<div class="columns"><section><h2>Results and data quality</h2>{summary}</section><section><h2>Open simulated position</h2>{position_html}</section></div>
<section><h2>Completed simulated trades</h2>{trades}</section>
<section><h2>Historical paper proposals</h2><p class="caption">These decisions apply only to their recorded observation times and expire at the listed timestamps. Any manual trade requires fresh data and your own review.</p>{proposals}</section>
<div class="columns"><section><h2>Modeling assumptions</h2><ul>{assumption_html}<li>Equity and drawdown reflect accepted observations only; unobserved market moves are not measured.</li><li>Dollar summaries show six decimal places. JSON and CSV retain the engine's original precision.</li></ul></section><section><h2>Strategy configuration</h2>{config}</section></div>
<footer>Offline snapshot. Reload this page to see reports rewritten by a running watcher. No scripts, external assets, or network connections.<br>Exports: <a href="result.json">Full result JSON</a> · <a href="trades.csv">Trades CSV</a> · <a href="proposals.csv">Proposals CSV</a> · <a href="events.jsonl">Events JSONL</a> · <a href="report.md">Markdown report</a></footer>
</main></body></html>'''


def write_reports(result: dict, output_dir: str | Path) -> dict[str, str]:
    """Write an offline report bundle and return absolute paths by filename.

    Each file is atomically replaced through a private temporary sibling file.
    The bundle as a whole is not a filesystem transaction. JSON preserves the
    engine schema; CSV protects text fields against spreadsheet formulas.
    """
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Render everything before replacement, so serialization errors cannot
    # leave a mixture of newly rendered and previously generated reports.
    documents = {
        "result.json": json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        "trades.csv": _csv(result.get("trades", []), TRADE_FIELDS),
        "proposals.csv": _csv(result.get("proposals", []), PROPOSAL_FIELDS),
        "events.jsonl": "".join(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n" for event in result.get("events", [])),
        "report.md": _markdown(result),
        "dashboard.html": _dashboard(result),
    }
    paths = {}
    for filename, content in documents.items():
        destination = directory / filename
        _atomic_write(destination, content)
        paths[filename] = str(destination)
    return paths
