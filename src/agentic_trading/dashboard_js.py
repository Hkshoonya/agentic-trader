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
  const barW = Math.max(2, (w - 24) / points.length - 2);
  points.forEach((p, i) => {
    const x = 12 + i * (barW + 2);
    const barH = (p.notional / max) * (h - 44);
    ctx.globalAlpha = p.mode === 'shadow' ? 0.45 : 1;
    ctx.fillStyle = p.side === 'sell' ? '#ff5f6d' : '#35d07f';
    ctx.fillRect(x, h - 22 - barH, barW, barH);
  });
  ctx.globalAlpha = 1;
  ctx.strokeStyle = '#1e2836';
  ctx.beginPath(); ctx.moveTo(0, h - 22); ctx.lineTo(w, h - 22); ctx.stroke();
  ctx.fillStyle = '#7b8a9e'; ctx.font = '11px monospace';
  ctx.fillText('order notionals (max $' + max.toFixed(2) + ')', 12, h - 6);
}

async function refresh() {
  const [summary, curve, feed, orders] = await Promise.all([
    fetch('/api/summary').then(r => r.json()),
    fetch('/api/equity').then(r => r.json()),
    fetch('/api/journal?offset=' + offset).then(r => r.json()),
    fetch('/api/orders').then(r => r.json()),
  ]);
  setBadge('mode', summary.kill_switch ? 'kill switch' : summary.mode,
    summary.kill_switch ? 'kill' : (summary.mode === 'live' ? 'live' : 'shadow'));
  setBadge('stage', 'stage: ' + summary.promotion.stage,
    summary.promotion.stage === 'live' ? 'live'
      : summary.promotion.stage === 'probation' ? 'probation' : 'stage');
  setBadge('session', summary.session, summary.session_allowed ? 'stage' : 'shadow');
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
    'cap ' + num(Number(summary.current_equity || 0) * 0.20) + ' (20%) · session policy ' + summary.session_policy;
  const counts = summary.event_counts || {};
  document.getElementById('trades').textContent =
    (counts.accepted || 0) + ' / ' + (counts.placed || 0) + ' / ' + (counts.rejected || 0);
  const streak = summary.promotion.streak, need = summary.promotion.required_cycles || 1;
  document.getElementById('streak').textContent = streak + ' / ' + need;
  document.getElementById('streak-bar').style.width = Math.min(100, (streak / need) * 100) + '%';

  const a = summary.promotion.last_assessment || {};
  const ev = a.evidence || {};
  const reasons = a.reasons || [];
  document.getElementById('gate').innerHTML = (a.eligible === undefined)
    ? '<div class="sub">no assessment yet — run: agentic-trading evolve</div>'
    : '<div class="row"><span>eligible</span><b>' + (a.eligible ? 'YES' : 'not yet') + '</b></div>'
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

  feed.records.forEach(renderEvent);
  offset = feed.offset;
  drawChart(curve);
  renderOrders(orders);
}

function renderOrders(data) {
  const rows = (data && data.rows) || [];
  const body = document.querySelector('#orders tbody');
  const counts = (data && data.counts) || {};
  document.getElementById('orders-count').textContent =
    (counts.accepted || 0) + ' accepted · ' + (counts.placed || 0) + ' placed · '
    + (counts.rejected || 0) + ' rejected · ' + (counts.failed || 0) + ' failed';
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11" class="sub">no decisions yet</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const size = r.dollar_amount ? '$' + num(r.dollar_amount) :
      (r.quantity ? num(r.quantity, 6) + ' sh' : '—');
    const alerts = r.alerts && Object.keys(r.alerts).length
      ? Object.keys(r.alerts).join(', ') : 'none';
    const side = r.side ? '<span class="' + (r.side === 'buy' ? 'buy' : 'sell') + '">' + r.side + '</span>' : '—';
    const at = r.at ? new Date(r.at).toLocaleTimeString() : '—';
    return '<tr><td>' + at + '</td>'
      + '<td><span class="pill ' + r.event + '">' + r.event + '</span></td>'
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
