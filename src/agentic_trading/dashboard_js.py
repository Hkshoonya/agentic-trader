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
  const detail = r.reason || r.symbol
    || (Array.isArray(r.symbols) ? r.symbols.join(',') : (r.symbols ?? '')) || '';
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

// Why the order table can sit still while the cycle stream moves: this
// strategy decides once per UTC day, and the console should say so rather than
// leave the operator staring at four unchanged rows.
function renderCadence(cadence) {
  const box = document.getElementById('orders-cadence');
  if (!box || !cadence) return;
  const last = cadence.last_decision_at
    ? new Date(cadence.last_decision_at).toLocaleTimeString() : '—';
  box.textContent = 'decides ' + (cadence.schedule || (cadence.rebalance || '—'))
    + ' · last decision ' + last
    + ' · next ' + (cadence.next_decision_local || cadence.next_decision_at || '—')
    + (cadence.held && cadence.held.length ? ' · holding ' + cadence.held.join(', ') : '')
    + ' — ' + (cadence.explanation || '');
}

// The rule's live opinion, refreshed on its own slower timer: it reads the same
// bar files the strategy reads, so it is not free.
async function refreshCandidates() {
  try {
    const data = await fetch('/api/candidates').then(r => r.json());
    renderCandidates(data);
  } catch (err) { /* one panel failing must not blank the console */ }
}

function renderCandidates(data) {
  const body = document.querySelector('#candidates tbody');
  if (!body) return;
  const rows = (data && data.rows) || [];
  const note = document.getElementById('candidates-note');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="sub">'
      + ((data && data.error) || 'no bar history for the whitelist') + '</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const blocked = (r.blocked_by || []).join('; ');
    const vote = Number(r.vote || 0);
    const cls = r.selected && !blocked ? 'buy' : (blocked ? 'sell' : 'sub');
    return '<tr><td>' + r.symbol + (r.held ? ' <span class="sub">held</span>' : '') + '</td>'
      + '<td>' + vote.toFixed(2) + '</td>'
      + '<td>' + num(r.vol_pct, 1) + '%</td>'
      + '<td class="' + cls + '">' + (r.selected ? (blocked ? 'yes · held back' : 'yes') : 'no') + '</td>'
      + '<td class="sell">' + (blocked || '—') + '</td>'
      + '<td class="sub">' + (r.reason || '') + (r.regime ? ' · regime ' + r.regime : '') + '</td></tr>';
  }).join('');
  if (note) {
    note.textContent = (data.note || '')
      + (data.generated_at ? ' · computed ' + new Date(data.generated_at).toLocaleTimeString() : '')
      + ' · in book now: ' + ((data.selected || []).join(', ') || 'nothing');
  }
}


// Decisions per day: what the agent actually produced, refusals included. The
// old chart drew accepted orders only, so a system that had never filled an
// order showed an empty canvas all day and read as "stuck".
function drawActivity(data) {
  const canvas = document.getElementById('chart');
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * ratio;
  canvas.height = canvas.clientHeight * ratio;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  const days = (data && data.days) || [];
  if (!days.length) {
    ctx.fillStyle = '#7b8a9e'; ctx.font = '12px monospace';
    ctx.fillText('no decisions recorded yet', 12, h / 2);
    return;
  }
  const totals = days.map(d => (d.accepted || 0) + (d.placed || 0)
    + (d.rejected || 0) + (d.failed || 0));
  const max = Math.max(...totals, 1);
  const plotH = h - 46;
  const slot = (w - 24) / days.length;
  const barW = Math.max(4, Math.min(30, slot - 6));
  days.forEach((d, i) => {
    const x = 12 + i * slot;
    const refused = (d.rejected || 0) + (d.failed || 0);
    const done = (d.accepted || 0) + (d.placed || 0);
    const refusedH = (refused / max) * plotH;
    const doneH = (done / max) * plotH;
    if (refused) {
      ctx.globalAlpha = 0.55;
      ctx.fillStyle = '#ff5f6d';
      ctx.fillRect(x, h - 24 - refusedH, barW, Math.max(1, refusedH));
      ctx.globalAlpha = 1;
    }
    if (done) {
      ctx.fillStyle = '#35d07f';
      ctx.fillRect(x, h - 24 - refusedH - doneH, barW, Math.max(1, doneH));
    }
    ctx.fillStyle = '#5c6775';
    ctx.font = '9px monospace';
    ctx.fillText(d.day.slice(5), x, h - 10);
    if (totals[i]) {
      ctx.fillStyle = '#8b97a8';
      ctx.fillText(String(totals[i]), x, h - 26 - refusedH - doneH - 2);
    }
  });
  ctx.strokeStyle = '#2a3342';
  ctx.beginPath(); ctx.moveTo(0, h - 24); ctx.lineTo(w, h - 24); ctx.stroke();
  const note = document.getElementById('activity-note');
  if (note && (data.reasons || []).length) {
    note.textContent = 'most common refusals: '
      + data.reasons.map(r => r[0] + ' ×' + r[1]).join(', ');
  }
}

// What sizing up costs: the evidence report's measured drawdown per per-order
// size, with the size in force marked and the 15% gate drawn across it.
function drawFrontier(data) {
  const canvas = document.getElementById('frontier');
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = canvas.clientWidth * ratio;
  canvas.height = canvas.clientHeight * ratio;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  const points = ((data && data.points) || []).filter(p => p.per_order_pct);
  if (!points.length) {
    ctx.fillStyle = '#7b8a9e'; ctx.font = '12px monospace';
    ctx.fillText('no evidence report yet — run: agentic-trading walkforward', 12, h / 2);
    return;
  }
  const ceiling = Number((data && data.ceiling_pct) || 15);
  const maxY = Math.max(ceiling * 1.3, ...points.map(p => Number(p.max_drawdown_pct || 0)));
  const maxX = Math.max(...points.map(p => Number(p.per_order_pct || 0)));
  const left = 42, bottom = h - 30, top = 14, right = w - 16;
  const px = (pct) => left + (Number(pct) / maxX) * (right - left);
  const py = (dd) => bottom - (Number(dd) / maxY) * (bottom - top);
  ctx.strokeStyle = '#2a3342';
  ctx.beginPath(); ctx.moveTo(left, top); ctx.lineTo(left, bottom); ctx.lineTo(right, bottom); ctx.stroke();
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = '#f0b429';
  ctx.beginPath(); ctx.moveTo(left, py(ceiling)); ctx.lineTo(right, py(ceiling)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#f0b429'; ctx.font = '9px monospace';
  ctx.fillText(ceiling + '% gate', left + 4, py(ceiling) - 4);
  points.forEach(p => {
    const x = px(p.per_order_pct), y = py(p.max_drawdown_pct);
    ctx.fillStyle = p.inside_ceiling ? '#35d07f' : '#ff5f6d';
    ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#8b97a8'; ctx.font = '9px monospace';
    ctx.fillText((Number(p.per_order_pct) * 100).toFixed(2) + '%', x - 12, bottom + 12);
    ctx.fillText(Number(p.max_drawdown_pct).toFixed(1) + '%', x - 10, y - 7);
  });
  const live = Number((data && data.live_pct) || 0);
  if (live > 0) {
    ctx.strokeStyle = '#dfe6f1';
    ctx.beginPath(); ctx.moveTo(px(live), top); ctx.lineTo(px(live), bottom); ctx.stroke();
    ctx.fillStyle = '#dfe6f1';
    ctx.fillText('live ' + (live * 100).toFixed(2) + '%', px(live) + 4, top + 10);
  }
  const costs = (data && data.costs) || {};
  const note = document.getElementById('frontier-note');
  if (note && costs.break_even_per_side_bps !== undefined) {
    note.textContent = 'break-even cost ' + num(Number(costs.break_even_per_side_bps))
      + ' bps/side vs ' + num(Number(costs.assumed_per_side_bps)) + ' assumed';
  }
}

// The arm control. It is *not shown* unless the system has reached the state
// that justifies it — promotion gate pass, stage at least probation, and a
// current evidence report — which is what the operator asked for: no button to
// press before the agent has earned the right to be armed. Disarming is always
// available, because lowering risk never needs permission.
async function setArmed(armed) {
  try {
    const response = await fetch(armed ? '/api/arm' : '/api/disarm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirm: armed ? 'ARM' : 'DISARM'}),
    });
    const data = await response.json();
    const box = document.getElementById('arm-status');
    if (box) {
      box.textContent = data.refused
        ? 'refused: ' + (data.reason || 'not eligible')
        : (armed ? 'armed — orders may be submitted' : 'disarmed');
    }
    refresh();
  } catch (err) {
    const box = document.getElementById('arm-status');
    if (box) box.textContent = 'could not reach the agent';
  }
}

function renderArm(summary) {
  const box = document.getElementById('arm');
  if (!box) return;
  const arm = summary.arm || {};
  const badge = document.getElementById('armed');
  if (badge) {
    badge.textContent = arm.armed ? 'ARMED' : 'DISARMED';
    badge.className = 'badge ' + (arm.armed ? 'kill' : '');
  }
  if (arm.armed) {
    box.innerHTML = '<button class="armbtn" onclick="setArmed(false)">Disarm</button>'
      + '<span class="sub" style="margin-left:8px">armed since '
      + (arm.since ? new Date(arm.since).toLocaleString() : '—') + '</span>';
    return;
  }
  // The pre-flight checklist is the whole point of "execute automatically after
  // proper checking": every item named, with its own verdict, before anything
  // reaches the broker.
  const checklist = (arm.checks || []).map(c =>
    '<div class="sub">' + (c.ok ? '✓' : '✗') + ' ' + c.name + ' — ' + c.detail + '</div>'
  ).join('');
  if (arm.auto_arm) {
    box.innerHTML = '<b class="' + (arm.passed ? 'buy' : 'sell') + '">AUTONOMOUS '
      + (arm.passed ? '· will arm on the next cycle' : '· held back') + '</b>'
      + '<div class="sub">' + (arm.reason || '') + '</div>' + checklist;
    return;
  }
  if (!arm.available) {
    // Not eligible: say why, and show nothing to click. A greyed-out button
    // invites a fight with the UI; the reason is the honest answer.
    box.innerHTML = '<span class="sub">not yet armed — ' + (arm.reason || '') + '</span>'
      + checklist;
    return;
  }
  box.innerHTML = '<button class="armbtn" onclick="setArmed(true)">Arm live trading</button>'
    + '<span class="sub" style="margin-left:8px">' + (arm.reason || '') + '</span>'
    + checklist;
}

// Runtime and money. Two clocks — this session, and every session this agent has
// ever run — and three P&L numbers, each labelled with the snapshot it is
// measured against, because "profit" without an origin is not actionable.
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm ' + sec + 's';
  return sec + 's';
}

function renderAccount(summary) {
  const box = document.getElementById('account');
  if (!box) return;
  const a = summary.account || {};
  const rt = a.runtime || {}, eq = a.equity || {}, pnl = a.pnl || {};
  const money = (v) => (v === null || v === undefined) ? '—'
    : (Number(v) >= 0 ? '+' : '') + '$' + Number(v).toFixed(2);
  const cls = (v) => (v === null || v === undefined) ? 'sub'
    : (Number(v) >= 0 ? 'buy' : 'sell');
  box.innerHTML =
    '<div class="row"><span>this session</span><b>' + fmtDuration(rt.session_seconds) + '</b></div>'
    + '<div class="row"><span>all time running</span><b>' + fmtDuration(rt.total_seconds)
      + (rt.sessions ? ' · ' + rt.sessions + ' start' + (rt.sessions === 1 ? '' : 's') : '')
      + '</b></div>'
    + '<div class="row"><span>equity now</span><b>$' + num(Number(eq.current || 0)) + '</b></div>'
    + '<div class="row"><span>P&amp;L all time</span><b class="' + cls(pnl.all_time) + '">'
      + money(pnl.all_time) + '</b></div>'
    + '<div class="row"><span>P&amp;L today</span><b class="' + cls(pnl.today) + '">'
      + money(pnl.today) + '</b></div>'
    + '<div class="row"><span>P&amp;L since arming</span><b class="' + cls(pnl.since_arming) + '">'
      + (pnl.since_arming === null || pnl.since_arming === undefined
        ? 'not armed yet' : money(pnl.since_arming)) + '</b></div>'
    + '<div class="sub">' + (eq.armed_at
      ? 'armed ' + new Date(eq.armed_at).toLocaleString() + ' at $' + num(Number(eq.at_arm || 0))
      : 'never armed — nothing has been submitted') + '</div>'
    + '<div class="sub">first reading $' + num(Number(eq.first || 0)) + ' on '
      + (eq.first_seen_at ? new Date(eq.first_seen_at).toLocaleString() : '—')
      + ' · P&amp;L is ' + (a.labels && a.labels.all_time ? a.labels.all_time : '') + '</div>'
    + '<div class="sub">shadow (simulated): ' + money(a.shadow && a.shadow.realized_total)
      + ' realized all time — ' + ((a.shadow && a.shadow.note) || '') + '</div>';
}

async function refresh() {
  let summary, curve, feed, orders, activity, frontier;
  try {
    [summary, curve, feed, orders, activity, frontier] = await Promise.all([
      fetch('/api/summary').then(r => r.json()),
      fetch('/api/equity').then(r => r.json()),
      fetch('/api/journal?offset=' + offset).then(r => r.json()),
      fetch('/api/orders').then(r => r.json()),
      fetch('/api/activity').then(r => r.json()),
      fetch('/api/frontier').then(r => r.json()),
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
  // Call out the case where the account cannot afford the size the evidence
  // allows: the guard refuses every entry, and five identical rejects are a bad
  // way to learn that.
  // Small-account mode: the cap has been raised above what the evidence supports
  // so that an order can be placed at all. It says so, and it says what that
  // costs in drawdown, because the operator authorised it but should not have to
  // remember it.
  const floor = risk.size_floor_active
    ? '<div class="row"><span>small-account mode</span><b class="sell">cap raised</b></div>'
      + '<div class="sub">' + num(Number(risk.max_order_pct || 0) * 100) + '% per order is '
      + num(Number(risk.order_at_ceiling || 0)) + ', below the '
      + num(Number(risk.min_order_notional || 0)) + ' minimum, so the cap is raised to '
      + num(Number(risk.effective_order_pct || 0) * 100) + '% ($'
      + num(Number(risk.effective_order_notional || 0)) + ' per order)'
      + (risk.drawdown_at_effective_pct !== null && risk.drawdown_at_effective_pct !== undefined
        ? ' — the walk-forward measures ' + num(Number(risk.drawdown_at_effective_pct))
          + '% max drawdown at that size'
          + (Number(risk.drawdown_at_effective_pct) <= 15
            ? ' (inside the 15% gate)'
            : ' (above the 15% gate — this is the trade you authorised)')
        : '')
      + '. It drops back to ' + num(Number(risk.max_order_pct || 0) * 100)
      + '% at $' + num(Number(risk.equity_needed || 0)) + ' equity.</div>'
    : (risk.too_small_to_trade
      ? '<div class="row"><span>account</span><b class="sell">too small to trade</b></div>'
        + '<div class="sub">a ' + num(Number(risk.min_order_notional || 0))
        + ' minimum order is ' + num(Number(risk.order_at_ceiling || 0))
        + ' at the current ' + num(Number(risk.max_order_pct || 0) * 100) + '% ceiling — '
        + 'entries are refused until equity reaches $'
        + num(Number(risk.equity_needed || 0)) + '</div>'
      : '');
  const tooSmall = floor;
  document.getElementById('gate').innerHTML = (a.eligible === undefined)
    ? budget + tooSmall + '<div class="sub">no assessment yet — run: agentic-trading evolve</div>'
    : budget + tooSmall
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
      + '<div class="row"><span>universe</span><b>'
        + ((e.symbols || []).length ? (e.symbols || []).join(',') : 'all bar files')
        + '</b></div>'
      + '<div class="row"><span>train / test bars</span><b>' + (e.train_bars ?? '—') + ' / ' + (e.test_bars ?? '—') + '</b></div>'
      + '<div class="row"><span>seed</span><b>' + (e.seed ?? '—') + '</b></div>'
      + '<div class="row"><span>champion</span><b>' + JSON.stringify(e.champion).slice(0, 90) + '</b></div>'
      + '<div class="sub" style="margin-top:8px">ran ' + (e.run_at || 'never') + '</div>';
  }
  // The evolution agent's queue: proposals for the operator to review. It can
  // write these and nothing else — no limit, no gate and no order changes.
  const proposals = summary.proposals || {};
  const proposalBox = document.getElementById('proposals');
  if (proposalBox) {
    if (!proposals.count) {
      proposalBox.innerHTML = 'nothing proposed yet — the agent proposes only when '
        + 'the telemetry shows something worth changing';
    } else {
      proposalBox.innerHTML =
        '<div class="sub">' + proposals.count + ' in the queue · model '
        + (proposals.model || '?') + ' · updated '
        + (proposals.updated_at ? new Date(proposals.updated_at).toLocaleString() : '—')
        + '</div>'
        + proposals.proposals.slice().reverse().map(p =>
          '<div class="row"><span>' + p.category + ' · ' + p.title + '</span><b class="sub">'
          + num(Number(p.confidence || 0), 2) + ' · ' + p.status + '</b></div>'
          + '<div class="sub" style="margin:-2px 0 6px">' + (p.evidence || '') + '</div>'
        ).join('');
    }
  }

  // The model's read on each symbol's regime, and whether it is holding
  // entries back. A "+trend" never creates a trade; only chop/panic stop one.
  const regimes = summary.regimes || {};
  renderEvidence(summary);
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

  // A stopped agent leaves every panel frozen at plausible values. Say it out
  // loud rather than letting a silent daemon look like a quiet market.
  if (summary.pulse) {
    const p = summary.pulse;
    const box = document.getElementById('pulse');
    const age = p.silent_seconds;
    if (p.silent) {
      box.style.display = '';
      box.className = 'badge kill';
      box.textContent = 'AGENT SILENT ' + Math.round(age / 60) + 'm'
        + (p.last_event_at ? ' (last event ' + new Date(p.last_event_at).toLocaleTimeString() + ')' : '');
    } else if (age !== null && age !== undefined) {
      box.style.display = '';
      box.className = 'badge';
      box.textContent = 'live · last event ' + Math.round(age) + 's ago';
    } else {
      box.style.display = 'none';
    }
  }

  feed.records.forEach(renderEvent);
  offset = feed.offset;
  drawChart(curve);
  drawActivity(activity);
  drawFrontier(frontier);
  renderOrders(orders);
  renderArm(summary);
  renderAccount(summary);
  renderCadence(summary.cadence);
}

// Who is actually making decisions: the bot is a small team of workers, and
// the daemon publishes their state so this is their roster, not a guess.
// The walk-forward evidence: the account's own history, priced at the size the
// bot would trade today versus the largest size the 15% drawdown ceiling allows.
// This is the number that decides whether "we can size up" is a fact or a mood.
function renderEvidence(summary) {
  const e = summary.evidence;
  const box = document.getElementById('evidence');
  if (!box) return;
  if (!e) {
    box.innerHTML = 'no evidence report yet — run: '
      + '<code>agentic-trading walkforward --config config/agentic.toml</code>';
    return;
  }
  const line = (name, s) => {
    if (!s) return '';
    return '<div class="row"><span>' + name + '</span><b>'
      + num(Number(s.per_order_pct || 0) * 100) + '% / order · '
      + (s.trades ?? '—') + ' trades · ' + num(s.expectancy_bps) + ' bps · DD '
      + num(s.max_drawdown_pct) + '% · $' + num(s.final_equity) + ' · p '
      + num(s.bootstrap_p_value, 4)
      + (s.eligible ? ' · inside gate' : '') + '</b></div>';
  };
  const age = e.generated_at
    ? Math.round((Date.now() - new Date(e.generated_at)) / 86400000) : null;
  box.innerHTML =
    line('risk-parity spec', e.inverse_vol)
    + line('gate size (in-sample)', e.gate_size)
    + line('production size', e.production)
    + '<div class="sub" style="margin-top:6px">'
      + (e.symbols || []).length + ' symbols · ' + (e.bars ?? '—') + ' bars · '
      + (e.folds ?? '—') + ' walk-forward folds · drawdown ceiling '
      + num(e.drawdown_ceiling_pct) + '%'
      + (age === null ? '' : ' · report ' + age + 'd old')
      + (e.auto_refresh_days
        ? ' · the daemon rebuilds it when older than ' + e.auto_refresh_days + 'd'
          + (e.refreshed_by_daemon ? ' (last rebuild was automatic)' : '')
        : '')
      + (e.gate_reason ? ' · ' + e.gate_reason : '')
    + '</div>'
    + (e.notes || []).map(note => '<div class="sub">note: ' + note + '</div>').join('');
}


function renderAgents(summary) {
  const agents = summary.agents || [];
  const box = document.getElementById('agents');
  if (!agents.length) {
    box.innerHTML = '<div class="sub">daemon has not published its roster yet</div>';
  } else {
    // The fleet: each agent's declared authority, its derived health, and when
    // it last did something. Health is computed from the work, so "running"
    // cannot be true of an agent that has silently stopped doing anything.
    const healthCls = {ok: 'buy', stale: 'sell', failing: 'sell',
                       degraded: 'shadow', disabled: 'sub', unknown: 'sub'};
    const authorityLabel = {read_only: 'read-only',
                            may_reduce_risk: 'reduce risk only',
                            may_trade: 'may trade'};
    const fmtAge = (seconds) => {
      if (seconds === null || seconds === undefined) return 'never';
      if (seconds < 90) return Math.round(seconds) + 's ago';
      if (seconds < 5400) return Math.round(seconds / 60) + 'm ago';
      return Math.round(seconds / 3600) + 'h ago';
    };
    box.innerHTML = agents.map(a => {
      const health = a.health || {};
      const status = health.status || a.status || 'unknown';
      const cls = healthCls[status] || 'sub';
      const bits = [];
      bits.push(fmtAge(health.age_seconds));
      if (authorityLabel[a.authority]) bits.push(authorityLabel[a.authority]);
      const d = a.detail || {};
      if (a.name === 'data' && d.symbols !== undefined) {
        bits.push(d.symbols + ' symbols synced');
      }
      if (a.name === 'research' && d.trades !== undefined) {
        bits.push(d.trades + ' trades · ' + num(Number(d.expectancy_bps || 0)) + ' bps');
      }
      if (a.name === 'strategy' && d.fresh_quotes !== undefined) {
        bits.push(d.fresh_quotes + ' fresh quotes/cycle');
      }
      if (a.name === 'execution') {
        bits.push(num(Number((d.max_order_pct || 0))) * 100 + '%/order');
        bits.push(d.armed ? 'ARMED' : 'disarmed');
      }
      if (a.name === 'backcheck' && d.ok !== undefined) {
        bits.push(d.ok + ' checks');
      }
      if ((a.consecutive_failures || 0) > 0) {
        bits.push(a.consecutive_failures + ' failures');
      }
      const err = (health.last_error || '').slice(0, 80);
      return '<div class="row"><span>' + a.name + '</span><b class="' + cls + '">'
        + status + '</b></div><div class="sub" style="margin:-2px 0 6px">'
        + bits.join(' · ') + (err ? ' · ' + err : '') + '</div>';
    }).join('')
      // The model helpers are not the fleet: they advise, they do not run.
      + (agents.some(a => a.name === 'advisor' || a.name === 'regime')
        ? '<div class="sub" style="margin-top:6px">supporting</div>' + agents
          .filter(a => a.name === 'advisor' || a.name === 'regime')
          .map(a => '<div class="row"><span>' + a.name + '</span><b class="sub">'
            + (a.status || '') + (a.model ? ' · ' + a.model : '') + '</b></div>')
          .join('')
        : '');
  }
  const alerts = summary.alerts || [];
  document.getElementById('alerts').innerHTML = alerts.length
    ? '<h2 style="margin-top:10px">Recent alerts sent</h2>' + alerts.slice().reverse()
        .map(a => '<div class="row"><span>' + (a.title || a.key) + '</span><b class="sub">'
          + ((a.channels || []).join('+') || 'no channel') + '</b></div>').join('')
    : '';
  // Back-check results: is the backend, the data and the analysis path sound?
  const health = summary.health || {};
  const healthBox = document.getElementById('health');
  if (health.checked_at === undefined) {
    healthBox.innerHTML = '';
  } else {
    const cls = health.healthy ? 'buy' : 'sell';
    const detail = health.healthy
      ? (health.ok || 0) + ' checks passed'
      : (health.failures || []).length + ' failing';
    const problems = (health.failures || []).concat(health.warnings || []);
    healthBox.innerHTML = '<h2 style="margin-top:10px">Back-check</h2>'
      + '<div class="row"><span>' + (health.healthy ? 'all systems' : 'attention')
      + '</span><b class="' + cls + '">' + detail + '</b></div>'
      + '<div class="sub">checked ' + new Date(health.checked_at).toLocaleTimeString() + '</div>'
      + problems.map(p => '<div class="sub">• <b>' + p.name + '</b> ' + p.detail + '</div>').join('');
  }
}

function renderOrders(data) {
  const rows = (data && data.rows) || [];
  const body = document.querySelector('#orders tbody');
  const counts = (data && data.counts) || {};
  document.getElementById('orders-count').textContent =
    (counts.accepted || 0) + ' accepted · ' + (counts.placed || 0) + ' placed · '
    + (counts.rejected || 0) + ' rejected · ' + (counts.failed || 0)
    + ' failed today (UTC)'
    + (data.older_rows
      ? ' · table shows ' + (data.rows || []).length + ' decisions over the last '
        + (data.days_shown || 3) + ' days (' + data.older_rows + ' older)'
      : '');
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
    // Rows can come from the last three journals, so an older row must show its
    // date — a bare 5:44 AM would read as this morning's decision.
    const at = r.at ? (r.older
      ? new Date(r.at).toLocaleString(undefined, {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'})
      : new Date(r.at).toLocaleTimeString()) : '—';
    // Three different questions, three numbers. `ev` is the strategy-level
    // evidence grade: it is computed from the evaluation and is identical for
    // every order until that evaluation changes. `order` is this order's own
    // grade and moves order to order; `ai` is the model's opinion.
    const c = r.confidence || {};
    const ai = (c.advisor === null || c.advisor === undefined) ? null : Number(c.advisor);
    const ev = (c.evidence === null || c.evidence === undefined) ? null : Number(c.evidence);
    const oc = c.order || {};
    const os = (oc.score === null || oc.score === undefined) ? null : Number(oc.score);
    const verdictClass = oc.verdict === 'buy' ? 'buy' : (oc.verdict === 'rejected' ? 'sell' : 'sub');
    const note = Object.values(oc.notes || {}).join('; ');
    // Rows journaled before per-order grading existed have no snapshot to grade
    // from. Saying so beats an empty cell that reads like a zero.
    const noGrade = !c.order && ev !== null;
    const hasVerdict = !!oc.verdict;
    const confidence = (ai === null && ev === null && os === null && !hasVerdict)
      ? '<span class="sub">—</span>'
      : '<span class="conf">'
        + (!hasVerdict ? ''
          : '<span class="' + verdictClass + '">' + oc.verdict
            + (os === null ? '' : ' ' + os.toFixed(2))
            // A rebuilt grade is not the reading the model was shown; say so on
            // the row rather than letting the two be compared as equals.
            + (oc.source === 'bars_asof' ? ' <span class="sub">as-of</span>' : '')
            + '</span>')
        + (os !== null && ai !== null ? '<br>' : '')
        + (ai === null ? ''
          : '<span class="' + (c.advisor_action === 'veto' ? 'sell' : 'buy') + '">ai '
            + ai.toFixed(2) + '</span>')
        + ((os !== null || ai !== null) && ev !== null ? '<br>' : '')
        + (ev === null ? '' : '<span class="sub">ev ' + ev.toFixed(2) + '</span>')
        + (note ? '<br><span class="sub">' + note.slice(0, 120) + '</span>' : '')
        + (noGrade ? '<br><span class="sub">order grade: no snapshot</span>' : '')
        + '</span>';
    return '<tr><td>' + at + '</td>'
      + '<td><span class="pill ' + r.event + '">' + r.event + '</span></td>'
      + '<td>' + confidence + '</td>'
      + '<td>' + (r.symbol || '—') + '</td>'
      + '<td>' + side + '</td>'
      + '<td>' + (r.type || '—') + '</td>'
      + '<td>' + (r.session || '—') + '</td>'
      // Only show a size and notional for an order that was actually sized:
      // a refusal before sizing has neither, and a blank is more honest than a
      // raw intent quantity beside $0.00.
      + '<td>' + (r.sized === false ? '—' : size) + '</td>'
      + '<td>' + (r.sized === false ? '—' : '$' + num(r.notional)) + '</td>'
      + '<td>' + (r.last_price
        ? '$' + num(r.last_price)
          + (r.last_price_source === 'decision' ? ' <span class="sub">dec</span>' : '')
        : '—') + '</td>'
      + '<td>' + alerts + '</td>'
      + '<td class="sub">' + (r.reason || '') + '</td></tr>';
  }).join('');
}

refresh();
refreshCandidates();
setInterval(refresh, 2000);
setInterval(refreshCandidates, 30000);
window.addEventListener('resize', () => fetch('/api/equity').then(r => r.json()).then(drawChart));
"""
