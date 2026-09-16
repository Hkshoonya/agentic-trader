"""Embedded dashboard UI (no external assets, no CDN)."""

from agentic_trading.dashboard_js import SCRIPT

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
.span3{grid-column:span 3}.span4{grid-column:span 4}.span5{grid-column:span 5}.span7{grid-column:span 7}.span8{grid-column:span 8}
.metric{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.sub{color:var(--muted);font-size:12px}
.row{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
.gauge{height:10px;border-radius:6px;background:#1b2431;overflow:hidden}
.gauge>div{height:100%;width:0;transition:width .6s ease;background:linear-gradient(90deg,var(--shadow),var(--accent))}
#stream{max-height:340px;overflow:auto}
.ev{display:grid;grid-template-columns:84px 1fr;gap:8px;padding:5px 0;border-bottom:1px dashed #1a2330}
.kind{font-weight:700}
.buy{color:var(--buy)}.sell{color:var(--sell)}.rejected{color:var(--warn)}.placed{color:var(--accent)}.shadow{color:var(--shadow)}
canvas{width:100%;height:200px;display:block}
.flash{animation:flash .7s ease}
@keyframes flash{from{background:#17263a}to{background:transparent}}
@media(max-width:900px){.span3,.span4,.span5,.span7,.span8{grid-column:span 12}}
</style></head>
<body>
<header><h1>Agentic Trader</h1>
<span id="mode" class="badge shadow">shadow</span>
<span id="stage" class="badge stage">stage: shadow</span>
<span id="session" class="badge">session</span>
<span id="kill" class="badge kill" style="display:none">kill switch</span>
<span class="sub" id="generated"></span></header>
<main>
<div class="card span3"><h2>Account equity</h2><div class="metric" id="equity">—</div><div class="sub" id="equity-sub">—</div></div>
<div class="card span3"><h2>Daily notional used</h2><div class="metric" id="notional">—</div><div class="sub" id="notional-sub">—</div></div>
<div class="card span3"><h2>Accepted / placed / rejected</h2><div class="metric" id="trades">0</div><div class="sub">today</div></div>
<div class="card span3"><h2>Promotion streak</h2><div class="metric" id="streak">0</div><div class="sub" id="streak-sub">assessments to next stage</div><div class="gauge" style="margin-top:8px"><div id="streak-bar"></div></div></div>
<div class="card span8"><h2>Order flow</h2><canvas id="chart"></canvas></div>
<div class="card span4"><h2>Promotion gate</h2><div id="gate"></div></div>
<div class="card span7"><h2>Live execution stream</h2><div id="stream"></div></div>
<div class="card span5"><h2>Evolution evidence</h2><div id="evolution" class="sub">no evolution run yet</div></div>
</main>
<script>
__SCRIPT__
</script></body></html>
"""

HTML = _TEMPLATE.replace("__SCRIPT__", SCRIPT)
