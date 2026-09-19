"""Dashboard client script (kept separate so the template stays readable)."""

SCRIPT = """
const num = (v, d=2) => (v === null || v === undefined || v === '') ? '—' : Number(v).toFixed(d);

// Dynamic values — symbols, reasons, error strings, model notes — reach the
// page through innerHTML, and some of them originate in a broker payload.
// Escape them, so a hostile value is text on the screen and never markup.
const esc = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

// A basis point is $0.01 per $100 traded. An operator reads dollars, not bps.
const perHundred = (bps) => {
  const value = Number(bps);
  if (bps === null || bps === undefined || bps === '' || !isFinite(value)) return '—';
  return (value >= 0 ? '+' : '−') + '$' + Math.abs(value / 100).toFixed(2) + ' per $100';
};

// "p = 0.113" is a statistic; "about 1 in 9" is the same fact in odds.
const plainLuck = (p) => {
  const value = Number(p);
  if (p === null || p === undefined || p === '' || !isFinite(value)) return '—';
  if (value <= 0) return 'no chance in the sample';
  if (value <= 0.002) return 'less than 1 in 500';
  return 'about 1 in ' + Math.max(2, Math.round(1 / value));
};

// The gate's own score, as a word. The number is still in the journal.
const plainScore = (score) => {
  const value = Number(score);
  if (score === null || score === undefined || score === '' || !isFinite(value)) return '—';
  if (value >= 0.75) return 'strong';
  if (value >= 0.5) return 'fair';
  if (value >= 0.25) return 'weak';
  return 'not convinced';
};

let offset = 0; const seen = new Set();

// The journal speaks in codes because codes can be counted, replayed and
// tested. The console speaks to a person. Every machine word that would reach
// the screen passes through these maps, so nobody has to learn what
// "below_min_notional" or "cycle_stats" means to read their own trading agent.
const EVENT_TEXT = {
  accepted: 'passed every check',
  placed: 'sent to broker',
  rejected: 'refused',
  place_failed: 'broker refused it',
  place_refused: 'not sent',
  live_gate_blocked: 'waiting for you to arm',
  decision_deferred: 'decided on the next cycle',
  rebalance_retry: 're-deciding after a technical refusal',
  session_closed: 'market closed',
  crypto_session: 'crypto still trading',
  cycle_stats: 'heartbeat',
  history_sync: 'price history updated',
  history_sync_failed: 'price history update failed',
  universe_change: 'watched symbols changed',
  universe: 'symbol scan',
  selfcheck: 'back-check',
  selfcheck_failed: 'back-check failed',
  error_streak: 'repeat errors',
  error_streak_cleared: 'errors cleared',
  kill_switch: 'kill switch tripped',
  open_orders: 'orders still filling',
  notify: 'alert sent',
  advisor: 'AI review',
  advisor_error: 'AI review unavailable',
  advisor_budget: 'AI review budget reached',
  entry_context: 'AI entry check',
  entry_context_failed: 'AI entry check failed',
  regime: 'market read',
  regime_failed: 'market read failed',
  evaluation: 'evidence review',
  evaluation_skipped: 'evidence unchanged',
  evolution_failed: 'evidence run failed',
  evolution_skipped: 'evidence run skipped',
  caps_applied: 'risk budget updated',
  promotion: 'stage raised',
  demotion: 'stage lowered',
  resized: 'order resized',
  small_account_mode: 'small-account mode',
  strategy_seeded: 'positions loaded',
  strategy_reseeded: 'positions updated',
  strategy_seed_failed: 'could not load positions',
  recovered_after_stop: 'recovered after a stop',
  execution_costs: 'fill costs measured',
  execution_costs_failed: 'fill-cost measurement failed',
  correlations: 'correlation map updated',
  correlations_failed: 'correlation update failed',
  quote_read_failed: 'could not read quotes',
  stale_quotes_rejected: 'stale quotes ignored',
  equity_read_failed: 'could not read the balance',
  positions_read_failed: 'could not read holdings',
  review_failed: 'broker review failed',
  auto_arm_failed: 'auto-arm check failed',
  auto_arm: 'auto-armed',
  auto_disarm: 'auto-disarmed',
  self_improve_skipped: 'evidence run skipped',
};

const REASON_TEXT = {
  symbol_not_whitelisted: 'not on the watched list',
  over_max_order: 'bigger than the per-order limit',
  over_daily_notional: 'would use more than the daily limit',
  max_open_positions: 'already holding as many positions as allowed',
  correlated_exposure: 'would double up on a position already held',
  correlation_unknown: 'not enough shared history to measure the overlap',
  would_short: 'nothing held to sell',
  oversell: 'more than the position holds',
  kill_switch: 'the kill switch is on',
  positions_read_failed: 'holdings could not be read',
  max_orders_per_day: 'the daily order limit is reached',
  open_order_pending: 'an order for this symbol is still filling',
  below_min_notional: 'too small for the broker minimum order',
  unknown_side: 'unrecognised order side',
  guard_error: 'the risk check failed',
  order_invalid: 'the broker would refuse this order',
  llm_veto: 'the AI adviser blocked it',
  regime_block: 'the market read blocks new entries',
  jev_chase: 'this entry looks like chasing a move that already ran',
  session_closed_for_equities: 'the stock market is closed',
  crypto_requires_live_stage: 'crypto needs the top promotion stage',
  confidence_flat: 'confidence did not move',
  confidence_down: 'confidence fell',
  reset_after_demotion: 'budget reset after a demotion',
  ceiling_reconciled: 'budget matched the operator ceiling',
  budget_above_target: 'budget above the evidence target',
  live_daily_loss: 'the daily loss limit was hit',
  shadow_daily_loss: 'the simulated daily loss limit was hit',
  consecutive_broker_errors: 'the broker kept failing',
  consecutive_loop_error: 'the loop kept failing',
};

const REGIME_TEXT = {
  trend: 'trending',
  trend_up: 'trending up',
  trend_down: 'trending down',
  chop: 'choppy',
  panic: 'panicking',
  neutral: 'calm',
  unknown: 'unclear',
};

const STRATEGY_TEXT = {
  fixture: 'test fixture',
  spy_scalper: 'S&P 500 scalper',
  llm: 'AI multi-asset',
  trend_crypto: 'crypto trend following',
};

const SESSION_TEXT = {
  premarket: 'before the open',
  regular: 'regular hours',
  afterhours: 'after hours',
  overnight: 'overnight',
  weekend: 'weekend',
  holiday: 'market holiday',
};

const POLICY_TEXT = {
  regular: 'regular hours only',
  extended: 'pre-market and after-hours too',
  all: 'around the clock',
  any: 'any time',
};

const CONFIDENCE_TEXT = {
  confidence_up: 'confidence rose',
  confidence_down: 'confidence fell',
  confidence_flat: 'confidence did not move',
  reset_after_demotion: 'reset after a demotion',
  ceiling_reconciled: 'matched to your ceiling',
  budget_above_target: 'above the evidence target',
};

const AGENT_STATUS_TEXT = {
  ok: 'working',
  stale: 'not reporting',
  failing: 'failing',
  degraded: 'running slowly',
  disabled: 'switched off',
  unknown: 'no activity yet',
};

const ORDER_TYPE_TEXT = {
  market: 'market',
  limit: 'limit',
  stop_market: 'stop',
  stop_limit: 'stop-limit',
};

const STAGE_TEXT = {
  shadow: 'practice only',
  probation: 'small live trades',
  live: 'live',
};

// A candidate row's "why" comes from the rule's own arithmetic. Same number,
// fewer machine words.
function plainCandidateReason(text) {
  const t = String(text || '');
  let match = t.match(/^vote ([\\d.-]+) \\((\\d+)\\/(\\d+) horizons up\\)$/);
  if (match) {
    return match[2] === match[3]
      ? 'price is up on every timeframe'
      : 'price is up on ' + match[2] + ' of ' + match[3] + ' timeframes';
  }
  match = t.match(/^trend vote ([\\d.-]+) < ([\\d.-]+) \\((\\d+)\\/(\\d+) horizons up\\)$/);
  if (match) {
    return 'only ' + match[3] + ' of ' + match[4] + ' timeframes rising — needs ' + match[2];
  }
  match = t.match(/^only (\\d+) bars \\(needs (\\d+)\\)$/);
  if (match) return 'only ' + match[1] + ' days of prices — needs ' + match[2];
  match = t.match(/^ranked (\\d+) of (\\d+), only (\\d+) slots$/);
  if (match) {
    return 'ranked ' + match[1] + ' of ' + match[2] + ', and only ' + match[3] + ' slots are open';
  }
  if (/^no usable volatility estimate$/.test(t)) {
    return 'not enough movement to size it safely';
  }
  return humanise(t);
}

// What is holding a candidate back, from the console's own checks.
function plainBlocker(text) {
  const t = String(text || '');
  if (t === 'already held') return 'already holding it';
  const match = t.match(/^regime ([a-z_]+) c=([\\d.]+)$/i);
  if (match) {
    return 'market read: ' + plainRegime(match[1]) + ' (' + match[2] + ')';
  }
  return humanise(t);
}

// The pre-flight checklist is written for a developer; this is the same answer
// for the person deciding whether to arm an account.
function plainCheckDetail(text) {
  let t = String(text || '');
  t = t.replace(/last assessment eligible: (true|false)/i, (match, value) =>
    value.toLowerCase() === 'true'
      ? 'the last assessment passed' : 'the last assessment did not pass');
  t = t.replace(/^stage is (\\w+)$/i, (match, value) =>
    'stage: ' + (STAGE_TEXT[value] || humanise(value)));
  t = t.replace(/healthy=(true|false)/i, (match, value) =>
    value.toLowerCase() === 'true' ? 'checks passed' : 'problems found');
  t = t.replace(/^missing$/, 'no report yet');
  t = t.replace(/^clear$/, 'not tripped');
  t = t.replace(/^equity ([\\d.]+)$/, 'balance $1');
  t = t.replace(/^([\\d.]+) of ([\\d.]+)$/, (match, a, b) =>
    num(Number(a) * 100, 2) + '% per order, ceiling ' + num(Number(b) * 100, 2) + '%');
  return t;
}

function humanise(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/_/g, ' ').replace(/\\s+/g, ' ').trim()
    // A sentence the console has no specific translation for still should not
    // make the reader learn the evidence rig's vocabulary mid-sentence.
    .replace(/\\bout-of-sample\\b/gi, 'held-back')
    .replace(/\\bin-sample\\b/gi, 'practice')
    .replace(/\\bwalk-forward\\b/gi, 'held-back')
    .replace(/\\bdrawdown\\b/gi, 'worst dip')
    .replace(/\\bbootstrap p\\b/gi, 'chance it is luck');
}

function plainEvent(name) {
  return EVENT_TEXT[name] || humanise(name) || 'event';
}

// A champion search genome is a JSON blob of tuning words. The operator needs
// the shape of the rule, not the key names it is stored under.
const GENOME_TEXT = {
  mode: 'rule',
  lookback: 'looks back',
  entry_bps: 'enters at',
  tp_bps: 'takes profit at',
  sl_bps: 'stops out at',
  max_hold_bars: 'holds at most',
};

function plainGenome(genome) {
  if (!genome || typeof genome !== 'object') return '—';
  const parts = [];
  for (const key of Object.keys(genome)) {
    const label = GENOME_TEXT[key] || humanise(key);
    const value = genome[key];
    if (key === 'entry_bps' || key === 'tp_bps' || key === 'sl_bps') {
      // Basis points become the dollars-per-$100 an operator already reads.
      parts.push(label + ' ' + perHundred(value));
    } else if (key === 'max_hold_bars') {
      parts.push(label + ' ' + value + ' days');
    } else if (key === 'lookback') {
      parts.push(label + ' ' + value + ' days');
    } else {
      parts.push(label + ' ' + esc(String(value)));
    }
  }
  return parts.join(' · ');
}

function plainStatus(status) {
  return AGENT_STATUS_TEXT[status] || humanise(status) || 'unknown';
}

function plainStrategy(name) {
  return STRATEGY_TEXT[name] || humanise(name) || '—';
}

// Free-text sentences (the cadence note) name the strategy plugin directly.
function plainStrategyWord(text) {
  let out = String(text || '');
  Object.keys(STRATEGY_TEXT)
    .sort((a, b) => b.length - a.length)
    .forEach(key => {
      out = out.replace(new RegExp('\\\\b' + key + '\\\\b', 'g'), STRATEGY_TEXT[key]);
    });
  return out;
}

function plainSession(name) {
  return name ? (SESSION_TEXT[name] || humanise(name)) : '—';
}

function plainRegime(name) {
  return name ? (REGIME_TEXT[name] || humanise(name)) : '—';
}

// One refusal code, in a sentence. Parametrised reasons keep their detail, with
// the machine-shaped parts (bracketed lists, "c=0.81") turned into prose.
function plainReason(reason) {
  const text = String(reason || '').trim();
  if (!text) return '';
  const cut = text.indexOf(':');
  const head = (cut >= 0 ? text.slice(0, cut) : text).trim();
  let detail = cut >= 0 ? text.slice(cut + 1).trim() : '';
  const phrase = REASON_TEXT[head] || humanise(head);
  if (!detail) return phrase;
  // A regime read arrives as the model's own sentence:
  // "jev-latest: panic 0.00, chop 0.06, trend_up 0.93, trend_down 0.01".
  // The keys are machine words, so they are translated wherever they appear
  // rather than only when the reason starts with a code this file knows.
  if (/(trend_up|trend_down|chop|panic)/.test(detail)) {
    const read = detail.replace(
      /\b(trend_up|trend_down|chop|panic)\b\s*([\d.]+)?/g,
      (match, name, value) =>
        plainRegime(name) + (value === undefined ? '' : ' ' + value)
    );
    // The stream already labels the row "market read"; this is the reading.
    return read;
  }
  if (head === 'regime_block') {
    const match = detail.match(/^([a-z_]+)\\s+c=([\\d.]+)$/i);
    if (match) {
      return 'the market read says ' + plainRegime(match[1]) +
        ' (confidence ' + match[2] + ')';
    }
  }
  if (head === 'jev_chase') {
    const match = detail.match(/^p=([\\d.]+)$/i);
    if (match) return phrase + ' (chance ' + match[1] + ')';
  }
  const list = detail.match(/^\\[(.*)\\]$/);
  if (list) {
    const names = list[1].split(',')
      .map(part => part.trim().replace(/^['"]|['"]$/g, ''))
      .filter(Boolean);
    if (names.length === 1) detail = names[0];
    else if (names.length > 1) {
      detail = names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
    }
  }
  return phrase + ' (' + detail + ')';
}

// The promotion gate's reasons are policy sentences written for the journal.
// They are correct and they are unreadable; this is the same fact in the words
// an operator would use.
function plainGateReason(reason) {
  const text = String(reason || '').trim();
  if (!text) return '';
  let match = text.match(/^only (\\d+)\\/(\\d+) out-of-sample folds profitable \\(need (\\d+)%\\)$/);
  if (match) {
    return 'only ' + match[1] + ' of ' + match[2] + ' held-back periods made money ' +
      '(needs ' + match[3] + '%)';
  }
  match = text.match(/^(?:in-sample|out-of-sample|walk-forward) sample too small \\((\\d+) trades < (\\d+)\\)$/);
  if (match) return 'only ' + match[1] + ' trades in the held-back test (needs ' + match[2] + ')';
  match = text.match(/(?:out-of-sample|walk-forward) expectancy ([-\\d.]+)bps < required ([-\\d.]+)bps after costs/);
  if (match) {
    return 'the held-back test earned ' + perHundred(match[1]) +
      ' traded; it needs ' + perHundred(match[2]) + ' traded to clear costs';
  }
  match = text.match(/edge does not survive the search: p=([\\d.]+) > [\\d.]+ \\(0\\.05 \\/ (\\d+) hypotheses tested\\)/);
  if (match) {
    return 'the edge may be luck: after testing ' + match[2] +
      ' variations the odds are too weak (p=' + match[1] + ')';
  }
  match = text.match(/edge indistinguishable from noise \\(bootstrap p=([\\d.]+) > [\\d.]+\\)/);
  if (match) return 'the edge looks like noise (p=' + match[1] + ')';
  match = text.match(/(?:out-of-sample|walk-forward) drawdown ([-\\d.]+)% > allowed ([-\\d.]+)%/);
  if (match) return 'the held-back test fell ' + match[1] + '%; the limit is ' + match[2] + '%';
  match = text.match(/walk-forward report is ([\\d.]+) days old \\(limit ([\\d.]+)\\)/);
  if (match) return 'the evidence report is ' + match[1] + ' days old (refresh after ' + match[2] + ')';
  match = text.match(/no size holds the ([\\d.]+)% drawdown ceiling on this history/);
  if (match) return 'no position size kept the worst fall under ' + match[1] + '% on this history';
  match = text.match(/trading ([\\d.]+)% per order but the evidence only supports ([\\d.]+)% inside the drawdown ceiling/);
  if (match) return 'trading ' + match[1] + '% per order, but the evidence only supports ' + match[2] + '%';
  match = text.match(/profit factor ([\\d.]+) < 1\\.0 despite positive expectancy/);
  if (match) return 'wins are smaller than losses (profit factor ' + match[1] + ')';
  if (/^walk-forward report has no usable timestamp$/.test(text)) {
    return 'the evidence report has no date on it, so it cannot be trusted';
  }
  if (/^no (walk-forward|out-of-sample) folds were evaluated$/.test(text)) {
    return 'the held-back test produced no periods to judge';
  }
  if (/^the agent is still in shadow/.test(text)) {
    return 'the agent is still practising: it has not cleared its evidence gate';
  }
  return humanise(text);
}

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
  const kind = r.event === 'accepted'
    ? (r.side || 'accepted') : plainEvent(r.event);
  const cls = r.event === 'placed' ? 'placed'
    : r.event === 'rejected' ? 'rejected'
    : r.side === 'buy' ? 'buy' : r.side === 'sell' ? 'sell'
    : (r.mode === 'shadow' ? 'shadow' : '');
  const detail = plainReason(r.reason) || r.symbol
    || (Array.isArray(r.symbols) ? r.symbols.join(', ') : (r.symbols ?? '')) || '';
  const amount = r.notional ? ' $' + num(r.notional) : (r.count ? ' x' + r.count : '');
  const when = r.at ? new Date(r.at).toLocaleTimeString() : new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = 'ev flash';
  div.innerHTML = '<span class="sub">' + when + '</span><span><span class="kind ' + cls + '">'
    + esc(kind) + '</span> ' + esc(detail) + amount + '</span>';
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
    + maxCum.toFixed(2) + ' · ' + points.length
    + (points.length === 1 ? ' decision' : ' decisions'), 12, h - 6);
}

// Why the order table can sit still while the cycle stream moves: this
// strategy decides once per UTC day, and the console should say so rather than
// leave the operator staring at four unchanged rows.
function renderCadence(cadence) {
  const box = document.getElementById('orders-cadence');
  if (!box || !cadence) return;
  const last = cadence.last_decision_at
    ? new Date(cadence.last_decision_at).toLocaleTimeString() : '—';
  box.textContent = 'decides ' + plainStrategyWord(cadence.schedule || (cadence.rebalance || '—'))
    + ' · last decision ' + last
    + ' · next ' + (cadence.next_decision_local || cadence.next_decision_at || '—')
    + (cadence.held && cadence.held.length ? ' · holding ' + cadence.held.join(', ') : '')
    + ' — ' + plainStrategyWord(cadence.explanation || '');
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
      + esc((data && data.error) || 'no bar history for the whitelist') + '</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const blocked = (r.blocked_by || []).map(plainBlocker).join('; ');
    const vote = Number(r.vote || 0);
    const cls = r.selected && !blocked ? 'buy' : (blocked ? 'sell' : 'sub');
    return '<tr><td>' + esc(r.symbol) + (r.held ? ' <span class="sub">held</span>' : '') + '</td>'
      + '<td>' + (vote >= 0.99 ? 'up on every timeframe'
        : 'up on ' + Math.round(vote * 4) + ' of 4 timeframes') + '</td>'
      + '<td>' + num(r.vol_pct, 1) + '%</td>'
      + '<td class="' + cls + '">'
      + (r.selected ? (blocked ? 'held' : 'yes') : 'no') + '</td>'
      + '<td class="sell">' + esc(blocked || '—') + '</td>'
      + '<td class="sub reason">' + esc(plainCandidateReason(r.reason))
        + (r.regime ? ' · market read ' + plainRegime(r.regime) : '') + '</td></tr>';
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
      + data.reasons.map(r => plainReason(r[0]) + ' ×' + r[1]).join(', ');
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
  // Labels used to pile up on each other at the right edge and on the top
  // points. Every label now reserves a box and is skipped (or clamped inside
  // the plot) when it would collide with one already drawn.
  const placed = [];
  const label = (text, x, y, colour) => {
    ctx.font = '9px monospace';
    const width = ctx.measureText(text).width;
    const cx = Math.min(Math.max(x, left + 2), right - width - 2);
    const box = {x: cx - 2, y: y - 9, w: width + 4, h: 11};
    if (placed.some(b => box.x < b.x + b.w && box.x + box.w > b.x
      && box.y < b.y + b.h && box.y + box.h > b.y)) return false;
    placed.push(box);
    ctx.fillStyle = colour;
    ctx.fillText(text, cx, y);
    return true;
  };
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = '#f0b429';
  ctx.beginPath(); ctx.moveTo(left, py(ceiling)); ctx.lineTo(right, py(ceiling)); ctx.stroke();
  ctx.setLineDash([]);
  label(ceiling + '% gate', left + 4, py(ceiling) - 4, '#f0b429');
  label('0%', left - 24, bottom + 3, '#5c6775');
  label(maxY.toFixed(0) + '%', left - 26, top + 8, '#5c6775');
  points.forEach(p => {
    const x = px(p.per_order_pct), y = py(p.max_drawdown_pct);
    ctx.fillStyle = p.inside_ceiling ? '#35d07f' : '#ff5f6d';
    ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
    label((Number(p.per_order_pct) * 100).toFixed(2) + '%', x - 12, bottom + 12, '#8b97a8');
    label(Number(p.max_drawdown_pct).toFixed(1) + '%', x - 10, y - 7, '#8b97a8');
  });
  const live = Number((data && data.live_pct) || 0);
  if (live > 0) {
    ctx.strokeStyle = '#dfe6f1';
    ctx.beginPath(); ctx.moveTo(px(live), top); ctx.lineTo(px(live), bottom); ctx.stroke();
    label('in force ' + (live * 100).toFixed(2) + '%', px(live) + 4, top + 10, '#dfe6f1');
  }
  const costs = (data && data.costs) || {};
  const note = document.getElementById('frontier-note');
  if (note && costs.break_even_per_side_bps !== undefined) {
    let text = 'the size breaks even if trading costs stay under '
      + perHundred(costs.break_even_per_side_bps) + ' traded (the model assumes '
      + perHundred(costs.assumed_per_side_bps) + ')';
    // A measured round trip beats an assumption, and on a small account it is
    // usually much worse than the assumption — say so next to it.
    const measured = Number(costs.per_side_cost_bps);
    if (isFinite(measured) && measured > 0) {
      text += ' · a real round trip on this account measured '
        + perHundred(measured) + ' a side';
    }
    note.textContent = text;
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
    '<div class="sub">' + (c.ok ? '✓' : '✗') + ' ' + esc(c.name) + ' — '
      + esc(plainCheckDetail(c.detail)) + '</div>'
  ).join('');
  if (arm.auto_arm) {
    box.innerHTML = '<b class="' + (arm.passed ? 'buy' : 'sell') + '">AUTONOMOUS '
      + (arm.passed ? '· will arm on the next cycle' : '· held back') + '</b>'
      + '<div class="sub">' + esc(arm.reason || '') + '</div>' + checklist;
    return;
  }
  if (!arm.available) {
    // Not eligible: say why, and show nothing to click. A greyed-out button
    // invites a fight with the UI; the reason is the honest answer.
    box.innerHTML = '<span class="sub">not yet armed — ' + esc(arm.reason || '') + '</span>'
      + checklist;
    return;
  }
  box.innerHTML = '<button class="armbtn" onclick="setArmed(true)">Arm live trading</button>'
    + '<span class="sub" style="margin-left:8px">' + esc(arm.reason || '') + '</span>'
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
  const armState = summary.arm || {};
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
        ? (armState.armed ? 'waiting for the agent\u2019s balance' : 'not armed yet')
        : money(pnl.since_arming)) + '</b></div>'
    // The balance snapshot at arming is written by the running daemon, so a
    // console-only session has the arm file but no snapshot. Saying "never
    // armed" next to an ARMED badge is worse than saying what is missing.
    + '<div class="sub">' + ((eq.armed_at || (armState.armed ? armState.since : ''))
      ? 'armed since ' + new Date(eq.armed_at || armState.since).toLocaleString()
        + (eq.at_arm ? ' at $' + num(Number(eq.at_arm)) : '')
      : (armState.armed
        ? 'armed — waiting for the agent to record a balance'
        : 'not armed — no order has been sent')) + '</div>'
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
  setBadge('stage', 'stage: ' + (STAGE_TEXT[summary.promotion.stage] || humanise(summary.promotion.stage)),
    summary.promotion.stage === 'live' ? 'live'
      : summary.promotion.stage === 'probation' ? 'probation' : 'stage');
  setBadge('session', plainSession(summary.session), summary.session_allowed ? 'stage' : 'shadow');
  // Live mode with no arming switch looks identical to shadow in the numbers,
  // so state it plainly: this is the difference between "not proven" and
  // "proven but not armed".
  setBadge('armed',
    summary.armed ? 'LIVE ARMED' : 'disarmed',
    summary.armed ? 'live' : 'shadow');
  document.getElementById('kill').style.display = summary.kill_switch ? '' : 'none';
  const generated = document.getElementById('generated');
  generated.textContent =
    'updated ' + new Date(summary.generated_at).toLocaleTimeString()
    + ' · ' + plainStrategy(summary.strategy)
    + ' · watching ' + summary.symbols.length + ' symbols';
  generated.title = summary.symbols.join(', ');

  document.getElementById('equity').textContent = num(summary.current_equity);
  document.getElementById('equity-sub').textContent = 'baseline ' + num(summary.baseline_equity)
    + ' · autonomy: ' + summary.autonomy;
  const notional = Number(summary.daily_notional || 0);
  document.getElementById('notional').textContent = num(notional);
  document.getElementById('notional-sub').textContent =
    'cap ' + num(Number(summary.current_equity || 0)
      * Number((summary.risk || {}).daily_notional_pct || 0.20))
    + ' · trading ' + (POLICY_TEXT[summary.session_policy] || humanise(summary.session_policy));
  const counts = summary.event_counts || {};
  const checked = counts.accepted || 0;
  const sent = counts.placed || 0;
  document.getElementById('trades').textContent =
    checked + ' / ' + sent + ' / ' + (counts.rejected || 0);
  // "Accepted" means the agent's own checks passed, not that a broker saw it.
  // On a $50 account every order so far has been accepted and none sent, and
  // the card has to say so in words or it reads like trading that never
  // happened — which is exactly what the operator reported.
  const tradesSub = document.getElementById('trades-sub');
  if (tradesSub) {
    tradesSub.textContent = (checked > sent)
      ? 'checked / sent to broker / refused — ' + (checked - sent)
        + ' passed every check but were never sent'
      : 'checked / sent to broker / refused';
    tradesSub.style.color = (checked > sent) ? '#ffcc66' : '';
  }
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
      + num(Number(risk.max_order_pct || 0) * 100) + '% (ceiling '
      + num(Number(risk.ceiling_max_order_pct || 0) * 100) + '%)</b></div>'
    + '<div class="row"><span>budget / day</span><b>'
      + num(Number(risk.daily_notional_pct || 0) * 100) + '% (ceiling '
      + num(Number(risk.ceiling_daily_notional_pct || 0) * 100) + '%)</b></div>'
    + '<div class="row"><span>evidence strength</span><b>' + plainScore(risk.confidence)
      + (risk.reason
        ? ' · ' + esc(CONFIDENCE_TEXT[risk.reason] || humanise(risk.reason))
        : '') + '</b></div>'
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
      + '<div class="row"><span>cleared the gate</span><b>' + (a.eligible ? 'yes' : 'not yet') + '</b></div>'
      + '<div class="row"><span>how convinced the checks are</span><b>' + plainScore(a.score) + '</b></div>'
      + '<div class="row"><span>orders checked on unseen dates</span><b>' + (ev.oos_trades ?? '—') + '</b></div>'
      + '<div class="row"><span>what each trade made</span><b>' + perHundred(ev.oos_expectancy_bps) + ' traded</b></div>'
      + '<div class="row"><span>held-back periods that made money</span><b>' + (ev.folds_positive ?? '—') + '/' + (ev.folds_total ?? '—') + '</b></div>'
      + '<div class="row"><span>chance it is luck</span><b>' + plainLuck(ev.oos_bootstrap_p_value) + '</b></div>'
      + (reasons.length
        ? '<div class="sub" style="margin-top:8px">'
          + reasons.map(r => '• ' + plainGateReason(r)).join('<br>') + '</div>'
        : '');

  if (summary.evolution) {
    const e = summary.evolution;
    document.getElementById('evolution').innerHTML =
      '<div class="row"><span>rules tried</span><b>' + (e.evaluated ?? '—') + '</b></div>'
      + '<div class="row"><span>symbols tested</span><b>'
        + ((e.symbols || []).length
            ? esc((e.symbols || []).join(','))
            : 'every symbol with price history')
        + '</b></div>'
      + '<div class="row"><span>days for practice / days held back</span><b>'
        + (e.train_bars ?? '—') + ' / ' + (e.test_bars ?? '—') + '</b></div>'
      + '<div class="row"><span>best rule found</span><b class="sub">'
        + esc(plainGenome(e.champion)) + '</b></div>'
      + '<div class="sub" style="margin-top:8px">ran ' + esc(e.run_at || 'never') + '</div>';
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
          '<div class="row"><span>' + esc(humanise(p.category)) + ' · '
          + esc(humanise(p.title)) + '</span><b class="sub">'
          + plainScore(p.confidence) + ' · ' + esc(humanise(p.status)) + '</b></div>'
          + '<div class="sub" style="margin:-2px 0 6px">'
          + esc(humanise(p.evidence || '')) + '</div>'
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
      return '<div class="row"><span>' + esc(sym) + '</span><b class="' + blocked + '">'
        + esc(plainRegime(r.regime)) + ' · '
        + num(Number(r.confidence || 0) * 100, 0) + '% sure'
        + (r.blocks_entries ? ' · blocking new entries' : '') + '</b></div>';
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
  renderUniverse(summary);
}

// The symbol scout: what the market's own lists put in front of it, and what it
// decided about each one. "Added" and "dropped" are the only two things that
// change the book, so they lead.
function renderUniverse(summary) {
  const box = document.querySelector('#universe tbody');
  const note = document.getElementById('universe-note');
  if (!box) return;
  const u = summary.universe || {};
  if (!u.enabled) {
    box.innerHTML = '<tr><td colspan="6" class="sub">the symbol scout is off — '
      + 'the watched list is the one written in your config file</td></tr>';
    if (note) note.textContent = '';
    return;
  }
  const rows = u.rows || [];
  if (!rows.length) {
    box.innerHTML = '<tr><td colspan="6" class="sub">the scout ran, but the '
      + 'market lists gave it nothing it could judge</td></tr>';
  } else {
    box.innerHTML = rows.map(r => {
      const volume = Number(r.median_dollar_volume || 0);
      const volumeText = volume >= 1e6
        ? '$' + (volume / 1e6).toFixed(1) + 'M'
        : (volume > 0 ? '$' + Math.round(volume / 1e3) + 'k' : '—');
      const spread = (r.spread_bps === null || r.spread_bps === undefined)
        ? '—' : perHundred(r.spread_bps) + ' to trade';
      const verdict = r.admitted
        ? '<span class="buy">added to the watch list</span>'
        : '<span class="sub">left out</span>';
      const vote = Number(r.vote || 0);
      const voteText = vote >= 0.99 ? 'up on every timeframe'
        : vote > 0 ? 'up on ' + Math.round(vote * 4) + ' of 4 timeframes'
        : 'no timeframe rising';
      return '<tr><td class="nowrap">' + esc(r.symbol) + '</td>'
        + '<td>' + voteText + '</td>'
        + '<td class="nowrap">' + volumeText + '</td>'
        + '<td class="nowrap">' + spread + '</td>'
        + '<td class="nowrap">' + verdict + '</td>'
        + '<td class="sub reason">' + esc(r.reason || '') + '</td></tr>';
    }).join('');
  }
  if (note) {
    const changes = [];
    if ((u.added || []).length) changes.push('added ' + u.added.join(', '));
    if ((u.dropped || []).length) changes.push('dropped ' + u.dropped.join(', '));
    note.textContent =
      (changes.length ? changes.join(' · ') : 'no change on the last scan')
      + ' · adopted by the scout: ' + ((u.adopted || []).join(', ') || 'nothing yet')
      + (u.as_of ? ' · scanned ' + new Date(u.as_of).toLocaleTimeString() : '')
      + (u.failures
        ? ' · ' + u.failures + ' failed scans in a row: ' + (u.last_error || '')
        : '')
      + (u.notes && u.notes.length ? ' · ' + u.notes.join(' · ') : '');
  }
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
    // A size the report never measured has nothing to say; an empty row of
    // zeros and dashes reads like a result, which is worse than no row.
    if (!s || s.trades === null || s.trades === undefined) return '';
    return '<div class="row"><span>' + esc(name) + '</span><b>'
      + num(Number(s.per_order_pct || 0) * 100) + '% of the account per order · '
      + (s.trades ?? '—') + ' past orders</b></div>'
      + '<div class="sub" style="margin:-2px 0 6px">'
      + perHundred(s.expectancy_bps) + ' traded · worst dip '
      + num(s.max_drawdown_pct) + '% · ended at $' + num(s.final_equity)
      + ' · chance it was luck: ' + plainLuck(s.bootstrap_p_value)
      + (s.eligible ? ' · inside the 15% dip limit' : '') + '</div>';
  };
  const age = e.generated_at
    ? Math.round((Date.now() - new Date(e.generated_at)) / 86400000) : null;
  box.innerHTML =
    line('the size the rule suggests', e.inverse_vol)
    + line('the biggest size inside the 15% dip limit', e.gate_size)
    + line('the size running now', e.production)
    + '<div class="sub" style="margin-top:6px">'
      + (e.symbols || []).length + ' symbols · ' + (e.bars ?? '—') + ' days of prices · '
      + (e.folds ?? '—') + ' separate periods tested · worst dip allowed '
      + num(e.drawdown_ceiling_pct) + '%'
      + (age === null ? '' : ' · report ' + age + 'd old')
      + (e.auto_refresh_days
        ? ' · the daemon rebuilds it after ' + e.auto_refresh_days + ' days'
          + (e.refreshed_by_daemon ? ' (last rebuild was automatic)' : '')
        : '')
      + (e.gate_reason ? ' · ' + esc(plainGateReason(e.gate_reason)) : '')
    + '</div>'
    + (e.notes || []).map(note => '<div class="sub">note: '
        + esc(plainGateReason(note)) + '</div>').join('');
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
        bits.push(d.trades + ' trades tested · '
          + perHundred(d.expectancy_bps) + ' traded');
      }
      if (a.name === 'strategy' && d.fresh_quotes !== undefined) {
        bits.push(d.fresh_quotes + ' fresh quotes/cycle');
      }
      if (a.name === 'execution') {
        // num() returns a string; multiplying it by 100 printed "0%/order" for
        // every real budget. Format the number, then the unit.
        bits.push(num(Number(d.max_order_pct || 0) * 100, 2) + '% per order');
        bits.push(d.armed ? 'ARMED' : 'disarmed');
      }
      if (a.name === 'backcheck' && d.ok !== undefined) {
        bits.push(d.ok + ' checks');
      }
      if ((a.consecutive_failures || 0) > 0) {
        bits.push(a.consecutive_failures + ' failures');
      }
      const err = (health.last_error || '').slice(0, 80);
      return '<div class="row"><span>' + esc(a.name) + '</span><b class="' + cls + '">'
        + plainStatus(status) + '</b></div><div class="sub" style="margin:-2px 0 6px">'
        + esc(bits.join(' · ')) + (err ? ' · ' + esc(err) : '') + '</div>';
    }).join('')
      // The model helpers are not the fleet: they advise, they do not run.
      + (agents.some(a => a.name === 'advisor' || a.name === 'regime')
        ? '<div class="sub" style="margin-top:6px">supporting</div>' + agents
          .filter(a => a.name === 'advisor' || a.name === 'regime')
          .map(a => '<div class="row"><span>' + esc(a.name) + '</span><b class="sub">'
            + esc(a.status || '') + (a.model ? ' · ' + esc(a.model) : '') + '</b></div>')
          .join('')
        : '');
  }
  const alerts = summary.alerts || [];
  document.getElementById('alerts').innerHTML = alerts.length
    ? '<h2 style="margin-top:10px">Recent alerts sent</h2>' + alerts.slice().reverse()
        .map(a => '<div class="row"><span>' + esc(a.title || a.key) + '</span><b class="sub">'
          + esc((a.channels || []).join('+') || 'no channel') + '</b></div>').join('')
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
      + problems.map(p => '<div class="sub">• <b>' + esc(p.name) + '</b> '
          + esc(p.detail) + '</div>').join('');
  }
}

function renderOrders(data) {
  const rows = (data && data.rows) || [];
  const body = document.querySelector('#orders tbody');
  const counts = (data && data.counts) || {};
  document.getElementById('orders-count').textContent =
    (counts.accepted || 0) + ' passed every check · ' + (counts.placed || 0)
    + ' actually sent to the broker · ' + (counts.rejected || 0) + ' refused · '
    + (counts.failed || 0) + ' failed at the broker today (UTC)'
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
    const side = r.side
      ? '<span class="' + (r.side === 'buy' ? 'buy' : 'sell') + '">' + esc(r.side) + '</span>'
      : '—';
    // The pill's class comes from the record, so only the events the stylesheet
    // knows are allowed to name it.
    const eventName = String(r.event || '');
    const eventClass = ['accepted', 'placed', 'rejected', 'place_failed', 'place_refused']
      .includes(eventName) ? eventName : 'rejected';
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
    // The per-order notes are a sentence fragment per check ("trend slope is
    // negative (-5.2%); volume not measured..."), which turned the column into
    // a vertical word stack. Keep one line on screen, the rest in the tooltip.
    const note = Object.values(oc.notes || {}).map(plainReason).join('; ');
    // Rows journaled before per-order grading existed have no snapshot to grade
    // from. Saying so beats an empty cell that reads like a zero.
    const noGrade = !c.order && ev !== null;
    const hasVerdict = !!oc.verdict;
    // The grade as a phrase, not a number on screen: "looked good" / "borderline"
    // / "looked weak". The 0..1 score and the model's confidence stay in the
    // tooltip, which is where an audit belongs.
    const verdictText = {buy: 'looked good', marginal: 'borderline', weak: 'looked weak',
                         rejected: 'was refused'};
    const grade = hasVerdict ? (verdictText[oc.verdict] || humanise(oc.verdict)) : '';
    const tooltip = [
      os === null ? '' : 'order grade ' + os.toFixed(2),
      ai === null ? '' : 'AI confidence ' + ai.toFixed(2),
      ev === null ? '' : 'evidence grade ' + ev.toFixed(2),
      note,
    ].filter(Boolean).join(' · ');
    const confidence = (ai === null && ev === null && os === null && !hasVerdict)
      ? '<span class="sub">—</span>'
      : '<span class="conf"' + (tooltip ? ' title="' + esc(tooltip) + '"' : '') + '>'
        + (!hasVerdict ? ''
          : '<span class="' + verdictClass + '">' + esc(grade)
            // A rebuilt grade is not the reading the model was shown; say so on
            // the row rather than letting the two be compared as equals.
            + (oc.source === 'bars_asof' ? ' <span class="sub">rebuilt later</span>' : '')
            + '</span>')
        + (c.advisor_action === 'veto' ? '<br><span class="sell">AI said no</span>'
          : (ai !== null && ai < 0.5 ? '<br><span class="sub">AI was unsure</span>' : ''))
        + (noGrade ? '<br><span class="sub">no snapshot to grade</span>' : '')
        + (note ? '<br><span class="sub">' + esc(note.length > 64 ? note.slice(0, 64) + '…' : note) + '</span>' : '')
        + '</span>';
    return '<tr><td>' + at + '</td>'
      + '<td><span class="pill ' + eventClass + '">' + esc(plainEvent(r.event)) + '</span></td>'
      + '<td>' + confidence + '</td>'
      + '<td class="nowrap">' + esc(r.symbol || '—') + '</td>'
      + '<td>' + side + '</td>'
      + '<td class="nowrap">' + esc(ORDER_TYPE_TEXT[r.type] || r.type || '—') + '</td>'
      + '<td class="nowrap">' + plainSession(r.session) + '</td>'
      // Only show a size and notional for an order that was actually sized:
      // a refusal before sizing has neither, and a blank is more honest than a
      // raw intent quantity beside $0.00.
      + '<td>' + (r.sized === false ? '—' : size) + '</td>'
      + '<td>' + (r.sized === false ? '—' : '$' + num(r.notional)) + '</td>'
      + '<td>' + (r.last_price
        ? '$' + num(r.last_price)
          + (r.last_price_source === 'decision' ? ' <span class="sub">dec</span>' : '')
        : '—') + '</td>'
      + '<td>' + esc(alerts) + '</td>'
      + '<td class="sub reason">' + esc(plainReason(r.reason)) + '</td></tr>';
  }).join('');
}

refresh();
refreshCandidates();
setInterval(refresh, 2000);
setInterval(refreshCandidates, 30000);
window.addEventListener('resize', () => fetch('/api/equity').then(r => r.json()).then(drawChart));
"""
