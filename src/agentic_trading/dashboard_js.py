"""Dashboard client script (kept separate so the template stays readable)."""

SCRIPT = """
const num = (v, d=2) => (v === null || v === undefined || v === '') ? '—' : Number(v).toFixed(d);
let offset = 0; const seen = new Set();

function setBadge(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'badge ' + cls;
}

function renderEvent(r) {
  const key = r.decision_id || (r.event + '|' + r.at + '|' + (r.count || ''));
  if (seen.has(key)) return;
  seen.add(key);
  const stream = document.getElementById('stream');
  const kind = r.event === 'accepted' ? (r.side || 'accepted') : r.event;
  const cls = r.event === 'placed' ? 'placed'
    : r.event === 'rejected' ? 'rejected'
    : r.side === 'buy' ? 'buy' : r.side === 'sell' ? 'sell'
    : (r.mode === 'shadow' ? 'shadow' : '');
  const detail = r.reason || r.symbol || (r.symbols || []).join(',') || '';
  const amount = r.notional ? ' $' + num(r.notional) : (r.count ? ' x' + r.count : '');
  const when = r.at ? new Date(r.at).toLocaleTimeString() : new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'ev flash';
  div.innerHTML = '<span class="sub">' + when + '</span><span><span class="kind ' + cls + '">'
    + kind + '</span> ' + detail + amount + '</span>';
  stream.prepend(div);
  while (stream.childElementCount > 200) stream.removeChild(stream.lastChild);
}

function drawChart(curve) {
  const canvas = document.getElementById('chart');
  const ratio = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * ratio;
  canvas.height = canvas.clientHeight * ratio;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  const points = curve.points || [];
  if (!points.length) {
    ctx.fillStyle = '#7b8a9e'; ctx.font = '12px monospace';
    ctx.fillText('waiting for executions…', 12, h / 2);
    return;
  }
  const max = Math.max(...points.map(p => p.notional), 1);
  const maxCum = Math.max(...points.map(p => p.cumulative || 0), 1);
  // A single decision used to stretch one bar across the whole canvas. Cap the
  // width so a sparse day reads as a bar, not a slab.
  const barW = Math.min(26, Math.max(3, (w - 24) / points.length - 2));
  const plotH = h - 44;
  points.forEach((p, i) => {
    const x = 12 + i * (barW + 2);
    const barH = Math.max(1, (p.notional / max) * plotH);
    ctx.globalAlpha = p.mode === 'shadow' ? 0.45 : 1;
    ctx.fillStyle = p.side === 'sell' ? '#ff5f6d' : '#35d07f';
    ctx.fillRect(x, h - 22 - barH, barW, barH);
  });
  ctx.globalAlpha = 1;
  // Cumulative notional, as promised by the legend.
  ctx.strokeStyle = '#4aa8ff';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = 12 + i * (barW + 2) + barW / 2;
    const y = h - 22 - ((p.cumulative || 0) / maxCum) * plotH;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.strokeStyle = '#1e2836';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, h - 22); ctx.lineTo(w, h - 22); ctx.stroke();
  ctx.fillStyle = '#7b8a9e'; ctx.font = '11px monospace';
  ctx.fillText('order notional (max $' + max.toFixed(2) + ') · cumulative $'
    + maxCum.toFixed(2) + ' · ' + points.length + ' decisions', 12, h - 6);
}

async function refresh() {
  let summary, curve, feed, orders;
  try {
    [summary, curve, feed, orders] = await Promise.all([
      fetch('/api/summary').then(r => r.json()),
      fetch('/api/equity').then(r => r.json()),
      fetch('/api/journal?offset=' + offset).then(r => r.json()),
      fetch('/api/orders').then(r => r.json()),
    ]);
  } catch (err) {
    // The console is restarted by systemd on deploys and failures; an open tab
    // must say so and keep retrying instead of throwing on every poll.
    document.getElementById('generated').textContent =
      'console unreachable — retrying…';
    return;
  }
  setBadge('mode', summary.kill_switch ? 'kill switch' : summary.mode,
    summary.kill_switch ? 'kill' : (summary.mode === 'live' ? 'live' : 'shadow'));
  setBadge('stage', 'stage: ' + summary.promotion.stage,
    summary.promotion.stage === 'live' ? 'live'
      : summary.promotion.stage === 'probation' ? 'probation' : 'stage');
  setBadge('session', summary.session, summary.session_allowed ? 'stage' : 'shadow');
  // Live mode with no arming switch looks identical to shadow in the numbers,
  // so state it plainly: this is the difference between "not proven" and
  // "proven but not armed".
  setBadge('armed',
    summary.armed ? 'LIVE ARMED' : 'disarmed',
    summary.armed ? 'live' : 'shadow');
  document.getElementById('kill').style.display = summary.kill_switch ? '' : 'none';
  document.getElementById('generated').textContent =
    'updated ' + new Date(summary.generated_at).toLocaleTimeString()
    + ' · ' + summary.strategy + ' · ' + summary.symbols.join(',');

  document.getElementById('equity').textContent = num(summary.current_equity);
  document.getElementById('equity-sub').textContent = 'baseline ' + num(summary.baseline_equity)
    + ' · autonomy: ' + summary.autonomy;
  const notional = Number(summary.daily_notional || 0);
  document.getElementById('notional').textContent = num(notional);
  document.getElementById('notional-sub').textContent =
    'cap ' + num(Number(summary.current_equity || 0)
      * Number((summary.risk || {}).daily_notional_pct || 0.20))
    + ' · session policy ' + summary.session_policy;
  const counts = summary.event_counts || {};
  document.getElementById('trades').textContent =
    (counts.accepted || 0) + ' / ' + (counts.placed || 0) + ' / ' + (counts.rejected || 0);
  const streak = summary.promotion.streak, need = summary.promotion.required_cycles || 1;
  document.getElementById('streak').textContent = streak + ' / ' + need;
  document.getElementById('streak-bar').style.width = Math.min(100, (streak / need) * 100) + '%';

  const a = summary.promotion.last_assessment || {};
  const ev = a.evidence || {};
  const reasons = a.reasons || [];
  // The risk budget is what actually moves between assessments: the caps climb
  // toward the operator's ceiling as evidence confidence improves.
  const risk = summary.risk || {};
  const budget = '<div class="row"><span>budget / order</span><b>'
      + num(Number(risk.max_order_pct || 0) * 100) + '% of '
      + num(Number(risk.ceiling_max_order_pct || 0) * 100) + '% authorized</b></div>'
    + '<div class="row"><span>budget / day</span><b>'
      + num(Number(risk.daily_notional_pct || 0) * 100) + '% of '
      + num(Number(risk.ceiling_daily_notional_pct || 0) * 100) + '% authorized</b></div>'
    + '<div class="row"><span>confidence</span><b>' + num(risk.confidence, 3)
      + (risk.reason ? ' · ' + risk.reason : '') + '</b></div>'
    + (risk.target_max_order_pct
      ? '<div class="sub">next target '
        + num(Number(risk.target_max_order_pct) * 100) + '% per order</div>'
      : '');
  document.getElementById('gate').innerHTML = (a.eligible === undefined)
    ? budget + '<div class="sub">no assessment yet — run: agentic-trading evolve</div>'
    : budget
      + '<div class="row"><span>eligible</span><b>' + (a.eligible ? 'YES' : 'not yet') + '</b></div>'
      + '<div class="row"><span>score</span><b>' + num(a.score, 3) + '</b></div>'
      + '<div class="row"><span>OOS trades</span><b>' + (ev.oos_trades ?? '—') + '</b></div>'
      + '<div class="row"><span>OOS expectancy</span><b>' + num(ev.oos_expectancy_bps) + ' bps</b></div>'
      + '<div class="row"><span>profitable folds</span><b>' + (ev.folds_positive ?? '—') + '/' + (ev.folds_total ?? '—') + '</b></div>'
      + '<div class="row"><span>bootstrap p</span><b>' + num(ev.oos_bootstrap_p_value, 3) + '</b></div>'
      + (reasons.length ? '<div class="sub" style="margin-top:8px">' + reasons.map(r => '• ' + r).join('<br>') + '</div>' : '');

  if (summary.evolution) {
    const e = summary.evolution;
    document.getElementById('evolution').innerHTML =
      '<div class="row"><span>genomes evaluated</span><b>' + (e.evaluated ?? '—') + '</b></div>'
      + '<div class="row"><span>train / test bars</span><b>' + (e.train_bars ?? '—') + ' / ' + (e.test_bars ?? '—') + '</b></div>'
      + '<div class="row"><span>seed</span><b>' + (e.seed ?? '—') + '</b></div>'
      + '<div class="row"><span>champion</span><b>' + JSON.stringify(e.champion).slice(0, 90) + '</b></div>'
      + '<div class="sub" style="margin-top:8px">ran ' + (e.run_at || 'never') + '</div>';
  }
  // The model's read on each symbol's regime, and whether it is holding
  // entries back. A "+trend" never creates a trade; only chop/panic stop one.
  const regimes = summary.regimes || {};
  renderAgents(summary);
  const symbols = Object.keys(regimes);
  if (symbols.length) {
    const rows = symbols.sort().map(sym => {
      const r = regimes[sym] || {};
      const blocked = r.blocks_entries ? 'sell' : 'buy';
      return '<div class="row"><span>' + sym + '</span><b class="' + blocked + '">'
        + (r.regime || '—') + ' ' + num(r.confidence, 2)
        + (r.blocks_entries ? ' · blocking' : '') + '</b></div>';
    }).join('');
    document.getElementById('regimes').innerHTML =
      '<h2 style="margin-top:10px">LLM regime read</h2>' + rows;
  }

  feed.records.forEach(renderEvent);
  offset = feed.offset;
  drawChart(curve);
  renderOrders(orders);
}

// Who is actually making decisions: the bot is a small team of workers, and
// the daemon publishes their state so this is their roster, not a guess.
function renderAgents(summary) {
  const agents = summary.agents || [];
  const box = document.getElementById('agents');
  if (!agents.length) {
    box.innerHTML = '<div class="sub">daemon has not published its roster yet</div>';
  } else {
    box.innerHTML = agents.map(a => {
      const cls = a.status === 'running' ? 'buy'
        : (a.status === 'tripped' ? 'sell' : 'sub');
      let detail = '';
      if (a.name === 'advisor') {
        detail = (a.calls || 0) + ' calls · ' + (a.errors || 0) + ' errors';
      } else if (a.name === 'regime') {
        detail = (a.views || 0) + ' views'
          + (a.worker ? ' · worker live' : '');
      } else if (a.name === 'risk guard') {
        detail = num(Number(a.max_order_pct || 0) * 100) + '%/order · '
          + num(Number(a.daily_notional_pct || 0) * 100) + '%/day';
      } else if (a.name === 'evolution') {
        detail = 'stage ' + (a.stage || '?');
      } else if (a.name === 'notifier') {
        detail = (a.channels && a.channels.length ? a.channels.join('+') : 'none')
          + ' · ' + (a.sent || 0) + ' sent';
      } else {
        detail = a.role || '';
      }
      return '<div class="row"><span>' + a.name + '</span><b class="' + cls + '">'
        + a.status + '</b></div><div class="sub" style="margin:-2px 0 6px">'
        + detail + '</div>';
    }).join('');
  }
  const alerts = summary.alerts || [];
  document.getElementById('alerts').innerHTML = alerts.length
    ? '<h2 style="margin-top:10px">Recent alerts sent</h2>' + alerts.slice().reverse()
        .map(a => '<div class="row"><span>' + (a.title || a.key) + '</span><b class="sub">'
          + ((a.channels || []).join('+') || 'no channel') + '</b></div>').join('')
    : '';
}

function renderOrders(data) {
  const rows = (data && data.rows) || [];
  const body = document.querySelector('#orders tbody');
  const counts = (data && data.counts) || {};
  document.getElementById('orders-count').textContent =
    (counts.accepted || 0) + ' accepted · ' + (counts.placed || 0) + ' placed · '
    + (counts.rejected || 0) + ' rejected · ' + (counts.failed || 0) + ' failed';
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="12" class="sub">no decisions yet</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    // Crypto is measured in coins, never "shares"; label the unit the way the
    // exchange does (0.00001577 BTC).
    const symbol = r.symbol || '';
    const isCrypto = symbol.indexOf('-') >= 0 || /USD$/.test(symbol);
    const size = r.dollar_amount ? '$' + num(r.dollar_amount) :
      (r.quantity
        ? (isCrypto
          ? num(r.quantity, 8) + ' ' + symbol.split('-')[0]
          : num(r.quantity, 4) + ' sh')
        : '—');
    const alerts = r.alerts && Object.keys(r.alerts).length
      ? Object.keys(r.alerts).join(', ') : 'none';
    const side = r.side ? '<span class="' + (r.side === 'buy' ? 'buy' : 'sell') + '">' + r.side + '</span>' : '—';
    const at = r.at ? new Date(r.at).toLocaleTimeString() : '—';
    // Two different questions, two numbers: how real the edge looks (evidence)
    // and how sure the model was about this specific order (advisor).
    const c = r.confidence || {};
    const ai = (c.advisor === null || c.advisor === undefined) ? null : Number(c.advisor);
    const ev = (c.evidence === null || c.evidence === undefined) ? null : Number(c.evidence);
    const confidence = (ai === null && ev === null) ? '<span class="sub">—</span>'
      : '<span class="conf">'
        + (ai === null ? ''
          : '<span class="' + (c.advisor_action === 'veto' ? 'sell' : 'buy') + '">ai '
            + ai.toFixed(2) + '</span>')
        + (ai !== null && ev !== null ? '<br>' : '')
        + (ev === null ? '' : '<span class="sub">ev ' + ev.toFixed(2) + '</span>')
        + '</span>';
    return '<tr><td>' + at + '</td>'
      + '<td><span class="pill ' + r.event + '">' + r.event + '</span></td>'
      + '<td>' + confidence + '</td>'
      + '<td>' + (r.symbol || '—') + '</td>'
      + '<td>' + side + '</td>'
      + '<td>' + (r.type || '—') + '</td>'
      + '<td>' + (r.session || '—') + '</td>'
      + '<td>' + size + '</td>'
      + '<td>$' + num(r.notional) + '</td>'
      + '<td>' + (r.last_price ? '$' + num(r.last_price) : '—') + '</td>'
      + '<td>' + alerts + '</td>'
      + '<td class="sub">' + (r.reason || '') + '</td></tr>';
  }).join('');
}

refresh();
setInterval(refresh, 2000);
window.addEventListener('resize', () => fetch('/api/equity').then(r => r.json()).then(drawChart));
"""
