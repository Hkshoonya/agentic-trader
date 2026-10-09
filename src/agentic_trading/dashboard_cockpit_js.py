"""The cockpit: story cards, the strategy race, tiles, the ticker, member cards.

It polls ``/api/desk`` every five seconds while the page is visible, keeps the
last good picture when a poll fails, and animates only what changed. The pure
formatting helpers live in ``CockpitFmt`` so Node can test them.
"""

COCKPIT = r"""
const CockpitFmt = (() => {
  const pct = (v) => (!Number.isFinite(v) ? '—'
    : (v < 0 ? '−' : '+') + Math.abs(v).toFixed(2) + '%');
  const money = (v) => (!Number.isFinite(v) ? '—' : '$' + v.toFixed(2));
  const countdown = (ms) => {
    if (!Number.isFinite(ms) || ms <= 0) return 'due now';
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm ' + (s % 60) + 's';
  };
  // Label y positions pushed at least `gap` apart, order kept, inside [lo, hi].
  const spread = (ys, gap, lo, hi) => {
    const order = ys.map((y, i) => [y, i]).sort((a, b) => a[0] - b[0]);
    const out = order.map(([y]) => y);
    for (let i = 1; i < out.length; i++) out[i] = Math.max(out[i], out[i - 1] + gap);
    if (out.length) out[out.length - 1] = Math.min(out[out.length - 1], hi);
    for (let i = out.length - 2; i >= 0; i--) out[i] = Math.min(out[i], out[i + 1] - gap);
    if (out.length && out[0] < lo) {
      const shift = lo - out[0];
      for (let i = 0; i < out.length; i++) out[i] += shift;
    }
    const result = new Array(ys.length);
    order.forEach(([, index], k) => { result[index] = out[k]; });
    return result;
  };
  // The money ring: member weights, or the benchmark's legs while no strategy
  // holds capital (a ring that says only "benchmark 100%" says nothing).
  const moneyParts = (allocation, labelOf) => {
    if (!allocation) return [];
    const weights = allocation.weights || {};
    const funded = Object.entries(weights).filter(([n, w]) => n !== 'benchmark' && w > 0);
    if (!funded.length) {
      return Object.entries(allocation.legs || {})
        .map(([symbol, w]) => ({ name: symbol.replace('-USD', ''), value: w }));
    }
    return Object.entries(weights).filter(([, w]) => w > 0)
      .map(([name, w]) => ({ name: labelOf(name), value: w }));
  };
  const tickerKey = (items) => (items || []).map((i) => i.at + '|' + i.text).join('\n');
  // A view the server could not build carries `error`: it must not replace the
  // last good picture (it would say "the desk is off" when it is not).
  const accept = (data) => !!data && !data.error;
  // The top bar's health dot: red for the kill switch or a failing check,
  // amber for warnings, green when every check passed.
  const healthLevel = (summary) => {
    if (!summary) return 'unknown';
    if (summary.kill_switch) return 'bad';
    const health = summary.health;
    if (!health) return 'unknown';
    if (health.healthy === false || (health.failures || []).length) return 'bad';
    if ((health.warnings || []).length) return 'warn';
    return 'ok';
  };
  // A daily sample is labelled with the UTC day that just closed, so it is the
  // value at the end of that day, not its start.
  const sampleTime = (day) => Date.parse(day) + 86400000;
  // The venues gateway's contribution to the header dot.
  const venueLevel = (view) => {
    if (!view || !view.enabled) return 'unknown';
    const streams = view.streams || [], venues = view.venues || [];
    if ([...streams, ...venues].some((x) => x.status === 'auth_failed')) return 'bad';
    if (view.stale) return 'warn';
    if (streams.some((s) => ['stale', 'reconnecting'].includes(s.status))) return 'warn';
    if (venues.some((v) => v.status === 'error')) return 'warn';
    return 'ok';
  };
  const RANK = { unknown: 0, ok: 1, warn: 2, bad: 3 };
  const worst = (a, b) => ((RANK[a] || 0) >= (RANK[b] || 0) ? a : b);
  // The switchboard card's words.
  const REGIME_WORDS = { trending: 'trending', squeeze: 'squeeze', choppy: 'choppy · standing aside',
    unclear: 'unclear · standing aside', warming: 'warming up' };
  const regimeWord = (regime) => REGIME_WORDS[regime] || 'unknown';
  const signedPct = (x) => (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(2) + '%';
  const fastCompare = (view) => {
    const a = view && view.book, c = view && view.mirror;
    if (!a || a.return_pct == null) return 'no trades yet';
    let text = 'the same trades: ' + signedPct(a.return_pct) + ' at Alpaca';
    if (c && c.return_pct != null) text += ', ' + signedPct(c.return_pct) + ' at Coinbase';
    if (c && c.unpriced) text += ' (' + c.unpriced + ' not copied)';
    return text;
  };
  const fundedBadge = (would) => 'judged only — not yet funded'
    + (would == null ? '' : ' · would earn ' + Math.round(would * 100) + '%');
  const tradeLine = (coin) => {
    const t = coin && coin.trade;
    if (!t) return coin && coin.standing_aside ? 'standing aside' : 'watching for a setup';
    return t.playbook + ' open at ' + t.entry + ' · stop ' + t.stop
      + (t.pnl_pct == null ? '' : ' · ' + signedPct(t.pnl_pct));
  };
  const swarmHead = (view) => (view.alive || 0) + ' alive · ' + (view.trials || 0) + ' recipes tried · book '
    + (view.book && view.book.return_pct != null ? signedPct(view.book.return_pct) : 'not started')
    + (view.follow_gap_bps != null ? ' · follows at ' + signedPct(view.follow_gap_bps / 100) + ' vs paper' : '');
  const swarmBadge = (weight) => 'funded by results · ' + (weight == null
    ? 'its first allocation comes on a Monday' : 'desk weight ' + Math.round(weight * 100) + '%');
  const swarmLine = (a) => a.forward_days + ' days · ' + a.state
    + (a.state === 'contributing' ? ' ' + Math.round((a.share || 0) * 100) + '%' : '')
    + ' · ' + signedPct(a.excess_pct || 0) + ' vs 60/40';
  const upgradeLine = (v) => !v || v.enabled === false ? 'off — set [upgrade] enabled = true'
    : v.paused ? 'paused — ' + (v.reason || 'by the operator')
    : v.canary && v.canary.title ? 'watching ' + v.canary.title + ' until ' + v.canary.until
    : 'running daily';
  return { pct, money, countdown, spread, moneyParts, tickerKey, accept, healthLevel, sampleTime, venueLevel, worst,
    regimeWord, fastCompare, fundedBadge, tradeLine, swarmHead, swarmLine, swarmBadge, upgradeLine };
})();

const Cockpit = (() => {
  const PALETTE = ['#35d07f', '#ffb020', '#4aa8ff', '#c77dff', '#ff5f6d'];
  const BENCH_COLOR = '#7b8a9e';
  const LEG_COLORS = ['#4aa8ff', '#6b7cff', '#35d07f', '#ffb020', '#c77dff'];
  let last = null;          // the last good /api/desk payload
  let raceDomain = null;    // the y-range the race settled on; the next glides from it
  let prevNow = {};         // each member's last "now" value, for the glide
  let drawnOnce = false;    // lines draw in on the first render only
  let tickerSeen = '';      // what is on the tape now
  let lastTickerAt = '';    // newest item already shown, so newer ones flash
  let nextAt = NaN;         // next allocation, epoch ms
  const shown = {};         // last value each counter settled on

  const $ = (id) => document.getElementById(id);
  const colorsFor = (members) => {
    const out = {};
    let i = 0;
    for (const m of members) out[m.name] = m.is_benchmark ? BENCH_COLOR : PALETTE[i++ % PALETTE.length];
    return out;
  };
  const shortTime = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  function say(id, text) {
    const el = $(id);
    if (!el || !text || el.textContent === text) return;
    el.classList.remove('fade');
    void el.offsetWidth;  // restart the fade animation
    el.classList.add('fade');
    el.textContent = text;
  }

  function count(id, value, fmt) {
    const el = $(id);
    if (!el) return;
    if (!Number.isFinite(value)) { el.textContent = '—'; delete shown[id]; return; }
    const from = Number.isFinite(shown[id]) ? shown[id] : value;
    shown[id] = value;
    if (from === value || document.hidden) { el.textContent = fmt(value); return; }
    const t0 = performance.now();
    const step = (now) => {
      const k = (now - t0) / 600;
      el.textContent = fmt(Charts.tween(from, value, k));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function raceLines(data) {
    const members = data.members || [];
    const DAY = 86400000;
    const times = members.flatMap((m) => (m.series || []).map((p) => CockpitFmt.sampleTime(p[0])))
      .filter(Number.isFinite);
    const now = Date.parse(data.as_of);
    const origin = times.length ? Math.min(...times) : now;
    const nowX = Math.max((now - origin) / DAY, 0);
    const lines = members.map((m) => {
      const pts = (m.series || []).map(([d, v]) => [(CockpitFmt.sampleTime(d) - origin) / DAY, v]);
      if (Number.isFinite(m.now_pct)) pts.push([nowX, m.now_pct]);
      return { m, pts };
    });
    return { nowX, lines };
  }

  function emptyRace(svg, W, H, text) {
    svg.innerHTML = '<text class="empty" x="' + (W / 2) + '" y="' + (H / 2)
      + '" text-anchor="middle">' + esc(text) + '</text>';
  }

  function drawRace(data, domain, animateIn) {
    const svg = $('race');
    if (!svg) return;
    const W = svg.clientWidth || 640, H = svg.clientHeight || 320;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    if (!data.enabled) {
      emptyRace(svg, W, H, 'The race follows the strategy desk; this bot trades on its own (see Orders).');
      return;
    }
    const { nowX, lines } = raceLines(data);
    if (!lines.some((l) => l.pts.length)) {
      emptyRace(svg, W, H, 'The race fills in one point per day.');
      return;
    }
    // Labels and margins grow with the chart so a 4K screen is not read through
    // a keyhole; below ~700px wide they stay at their designed size.
    const u = Math.min(1.8, Math.max(1, Math.min(W / 700, H / 450)));
    // On a phone-width chart full names would take most of it: short names
    // ("Momentum", "Crypto") leave the lines the room.
    const narrow = W < 520;
    const pad = { l: (narrow ? 44 : 56) * u, r: narrow ? 128 : 180 * u, t: 16 * u, b: 24 * u };
    const sx = Charts.scale(0, Math.max(nowX, 1), pad.l, W - pad.r);
    const sy = Charts.scale(domain[0], domain[1], H - pad.b, pad.t);
    const colors = colorsFor(data.members);
    const racers = lines.filter((l) => !l.m.is_benchmark && Number.isFinite(l.m.now_pct));
    const leader = racers.length
      ? racers.reduce((a, b) => (b.m.now_pct > a.m.now_pct ? b : a)).m.name : null;
    const zero = sy(0).toFixed(1);
    let out = '<line class="zero" x1="' + pad.l + '" x2="' + (W - pad.r) + '" y1="' + zero + '" y2="' + zero + '"/>';
    for (const v of [domain[1], 0, domain[0]]) {
      out += '<text class="grid" font-size="' + (11 * u).toFixed(1) + '" x="' + (pad.l - 8 * u).toFixed(1)
        + '" y="' + (sy(v) + 4 * u).toFixed(1) + '" text-anchor="end">' + esc(CockpitFmt.pct(v)) + '</text>';
    }
    const ends = [];
    for (const { m, pts } of lines) {
      const d = Charts.linePath(pts, sx, sy);
      if (!d) continue;
      const drawIn = animateIn && !m.is_benchmark;  // pathLength would stretch the benchmark's dashes
      out += '<path class="line' + (m.is_benchmark ? ' bench' : '') + (drawIn ? ' drawin' : '') + '"'
        + (drawIn ? ' pathLength="1"' : '') + ' data-name="' + esc(m.name) + '" d="' + d
        + '" stroke="' + colors[m.name] + '"/>';
      out += '<path class="hit" data-name="' + esc(m.name) + '" d="' + d + '"/>';
      const tip = pts[pts.length - 1];
      const x = sx(tip[0]), y = sy(tip[1]);
      out += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + (4.5 * u).toFixed(1) + '" fill="' + colors[m.name] + '"'
        + (m.name === leader ? ' class="lead"' : '') + '/>';
      ends.push({ m, x, y, value: tip[1] });
    }
    const placed = CockpitFmt.spread(ends.map((e) => e.y), 16 * u, pad.t + 6 * u, H - pad.b);
    ends.forEach((e, i) => {
      out += '<text class="end" font-size="' + (12 * u).toFixed(1) + '" x="' + (e.x + 10 * u).toFixed(1) + '" y="' + (placed[i] + 4 * u).toFixed(1)
        + '" fill="' + colors[e.m.name] + '">' + esc((narrow ? e.m.label.split(' ')[0] : e.m.label) + ' ' + CockpitFmt.pct(e.value)) + '</text>';
    });
    svg.innerHTML = out;
  }

  function renderRace(data) {
    const lines = data.enabled ? raceLines(data).lines : [];
    const target = Charts.extent(lines.flatMap((l) => l.pts.map((p) => p[1])));
    const fromNow = prevNow;
    prevNow = Object.fromEntries((data.members || []).map((m) => [m.name, m.now_pct]));
    if (!drawnOnce || !raceDomain) {
      drawRace(data, target, true);
      raceDomain = target;
      drawnOnce = true;
      return;
    }
    const from = raceDomain;
    raceDomain = target;
    const moved = from[0] !== target[0] || from[1] !== target[1]
      || (data.members || []).some((m) => fromNow[m.name] !== m.now_pct);
    if (!moved || document.hidden) { drawRace(data, target, false); return; }
    const t0 = performance.now();
    const frame = (now) => {
      const k = Math.min(1, (now - t0) / 600);
      const members = (data.members || []).map((m) => ({
        ...m,
        now_pct: Number.isFinite(fromNow[m.name]) && Number.isFinite(m.now_pct)
          ? Charts.tween(fromNow[m.name], m.now_pct, k) : m.now_pct,
      }));
      const domain = [Charts.tween(from[0], target[0], k), Charts.tween(from[1], target[1], k)];
      drawRace({ ...data, members }, domain, false);
      if (k < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function installRaceHover() {
    const svg = $('race'), tip = $('race-tip');
    if (!svg || !tip) return;
    const clear = () => {
      tip.style.display = 'none';
      svg.querySelectorAll('path.line').forEach((p) => p.classList.remove('dim'));
    };
    svg.addEventListener('mousemove', (ev) => {
      const name = ev.target && ev.target.dataset ? ev.target.dataset.name : null;
      const m = name && last ? (last.members || []).find((x) => x.name === name) : null;
      if (!m) { clear(); return; }
      svg.querySelectorAll('path.line').forEach((p) => p.classList.toggle('dim', p.dataset.name !== m.name));
      const rows = (m.holdings || []).slice(0, 8)
        .map((h) => esc(h.symbol) + ' ' + esc(CockpitFmt.money(h.value))).join('<br>');
      tip.innerHTML = '<b>' + esc(m.label + ' ' + CockpitFmt.pct(m.now_pct)) + '</b><br>'
        + (rows || '<span class="sub">holds cash</span>');
      const box = svg.parentElement.getBoundingClientRect();
      tip.style.left = (ev.clientX - box.left + 14) + 'px';
      tip.style.top = (ev.clientY - box.top + 14) + 'px';
      tip.style.display = 'block';
    });
    svg.addEventListener('mouseleave', clear);
  }

  function renderTiles(data) {
    const account = data.account || {};
    count('t-account', account.value, CockpitFmt.money);
    const spark = $('t-spark');
    if (spark) {
      const series = (account.series || []).slice(-30).map((p, i) => [i, p[1]]);
      const values = series.map((p) => p[1]).filter(Number.isFinite);
      const W = spark.clientWidth || 200, H = spark.clientHeight || 44;
      spark.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      if (values.length > 1) {
        const d = Charts.linePath(series, Charts.scale(0, series.length - 1, 2, W - 2),
          Charts.scale(Math.min(...values), Math.max(...values), H - 4, 4));
        const up = values[values.length - 1] >= values[0];
        spark.innerHTML = '<path d="' + d + '" fill="none" stroke-width="2" stroke="'
          + (up ? '#35d07f' : '#ff5f6d') + '"/>';
      } else {
        spark.innerHTML = '';
      }
    }
    const trial = data.trial, ring = $('t-trial-ring'), trialNum = $('t-trial');
    if (trial && Number.isFinite(trial.day)) {
      if (trial.verdict && trial.verdict !== 'running') {
        delete shown['t-trial'];
        if (trialNum) trialNum.textContent = trial.verdict === 'keep' ? 'Keep' : 'Kill';
      } else {
        count('t-trial', Math.max(0, trial.days - trial.day), (v) => Math.ceil(v) + ' days left');
      }
      // Rebuilt only when the day changes: rebuilding every poll replayed the
      // sweep animation every five seconds.
      const ringKey = [Math.floor(trial.day), trial.days, trial.verdict].join('|');
      if (ring && ring.dataset.key !== ringKey) {
        ring.dataset.key = ringKey;
        const done = Math.min(360, (trial.day / trial.days) * 360);
        ring.innerHTML = '<path d="' + Charts.arcPath(50, 50, 36, 44, 0, 360) + '" fill="#1b2431"/>'
          + (done > 0 ? '<path class="sweep" d="' + Charts.arcPath(50, 50, 36, 44, 0, done) + '" fill="#4aa8ff"/>' : '')
          + '<text x="50" y="55" text-anchor="middle" fill="#dbe4f0" font-size="14">'
          + esc('day ' + Math.floor(trial.day)) + '</text>';
      }
    } else {
      count('t-trial', NaN, String);
      if (ring) { ring.innerHTML = ''; delete ring.dataset.key; }
    }
    const money = $('t-money');
    if (money) {
      const labelOf = (n) => { const m = (data.members || []).find((x) => x.name === n); return m ? m.label : n; };
      const parts = CockpitFmt.moneyParts(data.allocation, labelOf);
      const key = JSON.stringify(parts);
      if (money.dataset.key !== key) {
        money.dataset.key = key;
        const arcs = Charts.arcs(parts);
        const colour = (i) => LEG_COLORS[i % LEG_COLORS.length];
        const share = (a) => a.name + ' ' + Math.round(a.value * 100) + '%';
        money.innerHTML = '<g class="sweep">' + arcs.map((a, i) => '<path d="'
          + Charts.arcPath(50, 50, 30, 46, a.start, a.end) + '" fill="' + colour(i) + '"><title>'
          + esc(share(a)) + '</title></path>').join('') + '</g>';
        const legend = $('t-money-legend');
        if (legend) {
          legend.innerHTML = arcs.map((a, i) => '<span style="color:' + colour(i) + '">●</span> '
            + esc(share(a))).join(' · ');
        }
      }
    }
  }

  function tick() {
    if (document.hidden) return;
    const el = $('t-next');
    if (el) el.textContent = Number.isFinite(nextAt) ? CockpitFmt.countdown(nextAt - Date.now()) : '—';
  }

  function renderTicker(items) {
    const tape = $('tape');
    if (!tape) return;
    const key = CockpitFmt.tickerKey(items);
    if (key === tickerSeen) return;  // rebuilding would restart the scroll
    tickerSeen = key;
    if (!items.length) {
      tape.innerHTML = '<span class="item">' + esc('No desk news yet: fills, orders and allocations will scroll here.') + '</span>';
      return;
    }
    tape.innerHTML = items.map((i) => '<span class="item ' + esc(i.kind)
      + (lastTickerAt && i.at > lastTickerAt ? ' new' : '') + '"><i class="dot"></i>'
      + esc(shortTime(i.at) + '  ' + i.text) + '</span>').join('');
    lastTickerAt = items[0].at;
    tape.style.animationDuration = Math.max(30, items.length * 6) + 's';
  }

  function renderMembers(data) {
    const box = $('members');
    if (!box) return;
    if (!data.enabled) {
      box.innerHTML = '<div class="sub">' + esc('The desk is off: this bot runs '
        + (data.strategy || 'one strategy') + ' on its own.') + '</div>';
      return;
    }
    const members = data.members || [];
    const colors = colorsFor(members);
    const bench = members.find((m) => m.is_benchmark);
    box.innerHTML = members.map((m) => {
      const gap = !m.is_benchmark && bench && Number.isFinite(m.now_pct) && Number.isFinite(bench.now_pct)
        ? ' ' + CockpitFmt.pct(m.now_pct - bench.now_pct).replace('%', ' pts') + ' vs buy-and-hold' : '';
      const need = m.samples_needed || 20;
      const fill = Math.min(100, Math.round(((m.samples || 0) / need) * 100));
      const chips = (m.holdings || []).map((h) => '<span class="chip">'
        + esc(h.symbol + ' ' + CockpitFmt.money(h.value)) + '</span>').join('')
        || '<span class="sub">holds cash</span>';
      const edge = Number.isFinite(m.t) ? m.t.toFixed(2) : 'not yet';
      const rule = m.is_benchmark ? '' : '<div class="sub">' + esc('Daily samples ' + (m.samples || 0) + ' of ' + need)
        + '</div><div class="progress"><div style="width:' + fill + '%"></div></div><div class="sub">'
        + esc('Edge score ' + edge + ' (must beat ' + Number(m.t_needed).toFixed(2) + ')') + '</div>';
      return '<div class="member" style="border-top-color:' + colors[m.name] + '"><h3>' + esc(m.label) + '</h3>'
        + '<div class="num" style="font-size:20px">' + esc(CockpitFmt.pct(m.now_pct))
        + '<span class="sub" style="font-size:12px">' + esc(gap) + '</span></div>'
        + '<div class="sub">' + esc('Capital ' + Math.round((m.weight || 0) * 100) + '% · '
        + m.entries + ' buys, ' + m.exits + ' sells') + '</div>'
        + '<div class="chips">' + chips + '</div>' + rule
        + '<div class="sub">' + esc(m.reason || '') + '</div></div>';
    }).join('');
  }

  let healthTitle = 'waiting for the first health check';

  function setLink(ok) {
    const dot = $('netdot');
    if (!dot) return;
    dot.classList.toggle('lost', !ok);
    dot.title = ok ? healthTitle : 'reconnecting… (showing the last good picture)';
  }

  let lastSummary = null;
  let lastVenues = null;

  function applyDot() {
    const dot = $('netdot');
    if (!dot) return;
    const own = CockpitFmt.healthLevel(lastSummary);
    const venues = CockpitFmt.venueLevel(lastVenues);
    const level = CockpitFmt.worst(own, venues);
    ['ok', 'warn', 'bad', 'unknown'].forEach((c) => dot.classList.toggle(c, c === level));
    const health = (lastSummary && lastSummary.health) || {};
    const names = (list) => (list || []).map((c) => c.name).join(', ');
    const parts = [];
    if (own === 'bad') parts.push(lastSummary && lastSummary.kill_switch ? 'kill switch engaged' : 'failing: ' + names(health.failures));
    else if (own === 'warn') parts.push('healthy, with warnings: ' + names(health.warnings));
    else if (own === 'ok') parts.push('every health check passed');
    if (venues === 'bad') parts.push('a venue login failed');
    else if (venues === 'warn') parts.push('a venue stream is stale or reconnecting');
    healthTitle = parts.join('; ') || 'health unknown';
    if (!dot.classList.contains('lost')) dot.title = healthTitle;
  }

  function setHealth(summary) {
    lastSummary = summary;
    applyDot();
  }

  function renderVenues(view) {
    const box = $('venues');
    if (!box) return;
    if (!view || !view.enabled) {
      box.innerHTML = '<div class="sub">' + esc((view && view.note) || 'the venues service is not running') + '</div>';
      return;
    }
    const venueRows = (view.venues || []).map((v) => '<div class="vrow"><span class="vstat ' + esc(v.status) + '"></span><b>'
      + esc(v.name) + '</b><span>' + esc(v.mode) + '</span><span>' + esc(v.armed ? 'ARMED' : 'disarmed') + '</span><span>'
      + esc(v.equity ? '$' + Number(v.equity).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '') + '</span><span class="sub">'
      + esc(v.last_error || v.status) + '</span></div>').join('');
    const streamRows = (view.streams || []).map((s) => '<div class="vrow"><span class="vstat ' + esc(s.status) + '"></span><b>'
      + esc(s.key) + '</b><span>' + esc(s.status) + '</span><span>' + esc(s.last_tick_age_s == null ? 'no tick yet' : 'last tick ' + s.last_tick_age_s + 's ago')
      + '</span><span>' + esc(s.delay_ms_median == null ? '' : 'delay ' + s.delay_ms_median + ' ms (p95 ' + s.delay_ms_p95 + ')')
      + '</span><span>' + esc('reconnects ' + s.reconnects) + '</span><span class="sub">' + esc(s.last_error || '') + '</span></div>').join('');
    box.innerHTML = (view.stale ? '<div class="sub">' + esc('the venues service has not reported for a while') + '</div>' : '')
      + venueRows + streamRows;
  }

  async function pollVenues() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/venues');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      lastVenues = await response.json();
    } catch (e) {
      lastVenues = null;  // unknown: the console itself may be restarting
    }
    renderVenues(lastVenues);
    applyDot();
  }

  function renderFast(view) {
    const box = $('fast');
    if (!box) return;
    if (!view || !view.enabled) {
      box.innerHTML = '<div class="sub">' + esc((view && view.note) || 'the switchboard is not running') + '</div>';
      return;
    }
    const head = '<div class="fhead"><span class="fbadge">' + esc(CockpitFmt.fundedBadge(view.would_earn))
      + '</span><span class="sub">' + esc(CockpitFmt.fastCompare(view)) + '</span></div>';
    let notes = '';
    if (view.failed) notes += '<div class="sub">' + esc('stopped: ' + view.failed) + '</div>';
    else if (view.stale) notes += '<div class="sub">' + esc('the switchboard has not reported for a while') + '</div>';
    if (view.halted) notes += '<div class="sub">' + esc('daily loss stop: no new entries until tomorrow (UTC)') + '</div>';
    const coins = (view.coins || []).map((c) => '<div class="fcoin"><b>' + esc(c.symbol) + '</b><span class="fchip '
      + esc(c.regime) + '">' + esc(CockpitFmt.regimeWord(c.regime)) + '</span><span>' + esc(CockpitFmt.tradeLine(c))
      + '</span></div>').join('');
    const recent = (view.recent || []).map((r) => '<li>' + esc(r.text) + '</li>').join('');
    box.innerHTML = head + notes + coins
      + (recent ? '<ul class="frecent">' + recent + '</ul>' : '<div class="sub">no decisions yet</div>');
  }

  async function pollFast() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/fast');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      renderFast(await response.json());
    } catch (e) {
      // keep the last picture; the header dot already shows a lost console
    }
  }

  function renderSwarm(view) {
    const box = $('swarm');
    if (!box) return;
    if (!view || !view.enabled) {
      box.innerHTML = '<div class="sub">' + esc((view && view.note) || 'the swarm has not run yet') + '</div>';
      return;
    }
    const head = '<div class="fhead"><span class="fbadge">' + esc(CockpitFmt.swarmBadge(view.weight))
      + '</span><span class="sub">' + esc(CockpitFmt.swarmHead(view)) + '</span></div>';
    const notes = view.stale ? '<div class="sub">' + esc(view.note || 'the swarm has not stepped for a while') + '</div>' : '';
    const tiles = (view.agents || []).map((a) => '<div class="stile ' + esc(a.state) + ' '
      + ((a.excess_pct || 0) >= 0 ? 'up' : 'down') + '"><b title="' + esc(a.family + ' · ' + a.universe + ' · ' + a.origin)
      + '">' + esc(a.name) + '</b><span class="sub">' + esc(CockpitFmt.swarmLine(a)) + '</span></div>').join('');
    const scout = view.scout && view.scout.enabled && view.scout.last_rationale
      ? '<div class="squote">' + esc('scout: “' + view.scout.last_rationale + '”') + '</div>' : '';
    const recent = (view.recent || []).map((r) => '<li>' + esc(r.text) + '</li>').join('');
    box.innerHTML = head + notes + (tiles ? '<div class="sgrid">' + tiles + '</div>' : '<div class="sub">no agents alive yet</div>')
      + scout + (recent ? '<ul class="frecent">' + recent + '</ul>' : '');
  }

  async function pollSwarm() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/swarm');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      renderSwarm(await response.json());
    } catch (e) {
      // keep the last picture; the header dot already shows a lost console
    }
  }

  function renderUpgrade(view) {
    const box = $('upgrade');
    if (!box || !view) return;
    const last = view.last || {};
    const shipped = view.last_shipped && view.last_shipped.title
      ? '<div class="sub">' + esc('last shipped: ' + view.last_shipped.title) + (view.last_shipped.pr
        ? ' · <a href="' + esc(view.last_shipped.pr) + '" target="_blank" rel="noopener">PR</a>' : '') + '</div>' : '';
    box.innerHTML = '<div class="fhead"><span class="fbadge">' + esc(CockpitFmt.upgradeLine(view)) + '</span></div>'
      + shipped + (last.message ? '<div class="sub">' + esc('latest: ' + last.message) + '</div>' : '')
      + '<div class="ubtns">' + (view.paused ? '<button data-act="resume">Resume</button>'
        : '<button class="stop" data-act="pause">Pause</button>')
      + '<button class="stop" data-act="rollback">Roll back last upgrade</button></div>';
    box.querySelectorAll('button[data-act]').forEach((b) => b.addEventListener('click', async () => {
      const act = b.getAttribute('data-act');
      if (act === 'rollback' && !window.confirm('Roll back the last automatic upgrade?')) return;
      try {
        const response = await fetch('/api/upgrade', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: act }) });
        if (response.ok) renderUpgrade(await response.json());
      } catch (e) {
        // the next poll shows the truth
      }
    }));
  }

  async function pollUpgrade() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/upgrade');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      renderUpgrade(await response.json());
    } catch (e) {
      // keep the last picture; the header dot already shows a lost console
    }
  }

  function render(data) {
    const story = data.story || {};
    say('say-now', story.right_now);
    say('say-money', story.money);
    say('say-just', story.just_now);
    const trial = data.trial, head = $('race-trial');
    if (head) {
      head.textContent = trial ? 'Day ' + Math.floor(trial.day) + ' of ' + trial.days + ' · '
        + trial.label + ' trial · verdict ' + trial.ends_at : '';
    }
    renderRace(data);
    renderTiles(data);
    nextAt = data.allocation ? Date.parse(data.allocation.next_at) : NaN;
    tick();
    renderTicker(data.ticker || []);
    renderMembers(data);
  }

  async function poll() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/desk');
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      if (!CockpitFmt.accept(data)) {
        // Keep the last good picture; say what went wrong in one place only.
        setLink(false);
        say('say-now', 'The cockpit could not read the desk just now ('
          + String((data && data.error) || 'no data') + '); showing the last good picture.');
        return;
      }
      last = data;
      setLink(true);
      render(data);
    } catch (e) {
      setLink(false);  // keep the last good picture on screen
    }
  }

  function start() {
    installRaceHover();
    poll();
    setInterval(poll, 5000);
    pollVenues();
    setInterval(pollVenues, 5000);
    pollFast();
    setInterval(pollFast, 2000);
    pollSwarm();
    setInterval(pollSwarm, 30000);
    pollUpgrade();
    setInterval(pollUpgrade, 30000);
    setInterval(tick, 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    // The overview's height is the screen minus the header, and the header's
    // height changes as its badges wrap, so it is measured, never assumed.
    const head = document.querySelector('header');
    const race = $('race');
    if (window.ResizeObserver) {
      if (head) {
        const setHead = () => document.documentElement.style.setProperty('--head', head.offsetHeight + 'px');
        new ResizeObserver(setHead).observe(head);
        setHead();
      }
      // Redraw whenever the chart's own box changes: a window resize, a tab
      // switch, or the header wrapping onto a second line.
      if (race) new ResizeObserver(() => { if (last) { drawRace(last, raceDomain || [-1, 1], false); renderTiles(last); } }).observe(race);
    } else {
      window.addEventListener('resize', () => { if (last) drawRace(last, raceDomain || [-1, 1], false); });
    }
  }

  return { start, poll, setHealth };
})();
"""

COCKPIT_BOOT = "Cockpit.start();\n"
