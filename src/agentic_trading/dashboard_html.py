"""Embedded dashboard UI (no external assets, no CDN)."""

from agentic_trading.dashboard_js import SCRIPT

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='13' font-size='13'>%F0%9F%93%88</text></svg>">
<title>Agentic Trader — live console</title>
<style>
:root{--bg:#0a0e14;--panel:#121821;--line:#1e2836;--text:#dbe4f0;--muted:#7b8a9e;--buy:#35d07f;--sell:#ff5f6d;--accent:#4aa8ff;--warn:#ffb020;--shadow:#6b7cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--line);background:#0d1219}
h1{font-size:15px;margin:0;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.badge{padding:4px 10px;border-radius:999px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;font-size:11px;border:1px solid transparent}
.badge.shadow{color:var(--shadow);border-color:var(--shadow)}
.badge.live{color:#04140b;background:var(--buy);animation:pulse 1.8s infinite}
.badge.probation{color:#1a1200;background:var(--warn);animation:pulse 1.8s infinite}
.badge.kill{color:#fff;background:var(--sell)}
.badge.stage{color:var(--accent);border-color:var(--accent)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
main{display:grid;gap:14px;padding:16px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{margin:0 0 10px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}
.metric{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.sub{color:var(--muted);font-size:12px}
.row{display:flex;justify-content:space-between;gap:10px;padding:3px 0;flex-wrap:wrap}
.row b{text-align:right;overflow-wrap:anywhere}
.gauge{height:10px;border-radius:6px;background:#1b2431;overflow:hidden}
.gauge>div{height:100%;width:0;transition:width .6s ease;background:linear-gradient(90deg,var(--shadow),var(--accent))}
#stream{max-height:340px;overflow:auto}
.ev{display:grid;grid-template-columns:84px 1fr;gap:8px;padding:5px 0;border-bottom:1px dashed #1a2330}
.kind{font-weight:700}
.buy{color:var(--buy)}.sell{color:var(--sell)}.rejected{color:var(--warn)}.placed{color:var(--accent)}.shadow{color:var(--shadow)}
canvas{width:100%;height:200px;display:block}
.legend{display:flex;gap:16px;margin-top:8px;color:var(--muted);font-size:11px}
.legend .dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}
.legend .dot.buy{background:var(--buy)}.legend .dot.sell{background:var(--sell)}
.legend .line{display:inline-block;width:14px;height:2px;background:var(--accent);margin-right:5px;vertical-align:middle}
.tablewrap{overflow:auto;max-height:340px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{position:sticky;top:0;background:#0f1620;color:var(--muted);text-align:left;font-weight:600;
   text-transform:uppercase;letter-spacing:.06em;font-size:10px;padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid #161f2b;font-variant-numeric:tabular-nums}
tr:hover td{background:#151d28}
.pill{padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;text-transform:uppercase}
.pill.accepted{background:#12301f;color:var(--buy)}
.pill.placed{background:#122a3d;color:var(--accent)}
.pill.rejected{background:#3a2a10;color:var(--warn)}
.pill.place_failed{background:#3a1418;color:var(--sell)}
.flash{animation:flash .7s ease}
@keyframes flash{from{background:#17263a}to{background:transparent}}
@media(max-width:900px){.span3,.span4,.span5,.span7,.span8,.span12{grid-column:span 12}}
</style></head>
<body>
<header><h1>Agentic Trader</h1>
<span id="mode" class="badge shadow">shadow</span>
<span id="stage" class="badge stage">stage: shadow</span>
<span id="session" class="badge">session</span>
<span id="armed" class="badge">arming…</span>
<span id="kill" class="badge kill" style="display:none">kill switch</span>
<span class="sub" id="generated"></span><span class="badge" id="pulse" style="display:none">—</span></header>
<main>
<div class="card span3"><h2>Account equity</h2><div class="metric" id="equity">—</div><div class="sub" id="equity-sub">—</div></div>
<div class="card span3"><h2>Daily notional used</h2><div class="metric" id="notional">—</div><div class="sub" id="notional-sub">—</div></div>
<div class="card span3"><h2>Accepted / placed / rejected</h2><div class="metric" id="trades">0</div><div class="sub">today</div></div>
<div class="card span3"><h2>Promotion streak</h2><div class="metric" id="streak">0</div><div class="sub" id="streak-sub">assessments to next stage</div><div class="gauge" style="margin-top:8px"><div id="streak-bar"></div></div></div>
<div class="card span8"><h2>Order flow · notional per decision &amp; cumulative</h2><canvas id="chart"></canvas>
  <div class="legend"><span><i class="dot buy"></i>buy</span><span><i class="dot sell"></i>sell</span><span><i class="line"></i>cumulative notional</span></div></div>
<div class="card span4"><h2>Promotion gate</h2><div id="gate"></div></div>
<div class="card span12"><h2>Market &amp; order table</h2>
  <div class="tablewrap"><table id="orders">
    <thead><tr><th>time</th><th>status</th><th>confidence</th><th>symbol</th><th>side</th><th>type</th><th>session</th><th>size</th><th>notional</th><th>last</th><th>alerts</th><th>reason</th></tr></thead>
    <tbody><tr><td colspan="12" class="sub">no decisions yet</td></tr></tbody>
  </table></div>
  <div class="sub" id="orders-count"></div>
  <div class="sub" id="orders-cadence"></div>
</div>
<div class="card span12"><h2>Candidates · what the rule wants right now</h2>
  <div class="tablewrap"><table id="candidates">
    <thead><tr><th>symbol</th><th>trend vote</th><th>vol (annualised)</th><th>in book</th><th>blocked by</th><th>reason</th></tr></thead>
    <tbody><tr><td colspan="6" class="sub">computing…</td></tr></tbody>
  </table></div>
  <div class="sub" id="candidates-note"></div>
</div>
<div class="card span7"><h2>Live execution stream</h2><div id="stream"></div></div>
<div class="card span5"><h2>Agents on duty</h2><div id="agents" class="sub">starting…</div><div id="alerts"></div><div id="health"></div></div>
<div class="card span5"><h2>Evolution evidence</h2><div id="evolution" class="sub">no evolution run yet</div><div id="regimes"></div></div>
<div class="card span12"><h2>Walk-forward evidence · what the order size is justified by</h2><div id="evidence" class="sub">no evidence report yet — run: agentic-trading walkforward --config config/agentic.toml</div></div>
</main>
<script>
__SCRIPT__
</script></body></html>
"""

HTML = _TEMPLATE.replace("__SCRIPT__", SCRIPT)
