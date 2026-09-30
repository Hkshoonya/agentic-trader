"""Pure SVG chart helpers for the cockpit: data in, numbers and path strings out.

Nothing here touches the DOM, so Node runs these directly in the tests. The
cockpit module draws with them.
"""

CHARTS = r"""
const Charts = (() => {
  // Map a value in [d0, d1] onto [r0, r1]; a flat domain maps to the middle.
  const scale = (d0, d1, r0, r1) => (v) =>
    d1 === d0 ? (r0 + r1) / 2 : r0 + (v - d0) * (r1 - r0) / (d1 - d0);

  // A padded [lo, hi] over the finite values, always including zero, so the
  // race is read against its starting line.
  const extent = (values, pad = 0.1) => {
    const finite = values.filter((v) => Number.isFinite(v));
    let lo = Math.min(0, ...finite), hi = Math.max(0, ...finite);
    if (lo === hi) { lo -= 1; hi += 1; }
    const room = (hi - lo) * pad;
    return [lo - room, hi + room];
  };

  // "M x y L x y ..." through the finite points; a gap starts a new segment.
  const linePath = (points, sx, sy) => {
    let out = '';
    let pen = false;
    for (const [x, y] of points) {
      if (!Number.isFinite(x) || !Number.isFinite(y)) { pen = false; continue; }
      const move = pen ? ' L ' : (out ? ' M ' : 'M ');
      out += move + sx(x).toFixed(1) + ' ' + sy(y).toFixed(1);
      pen = true;
    }
    return out;
  };

  // Donut segments in degrees, clockwise from 12 o'clock.
  const arcs = (parts) => {
    const clean = parts.filter((p) => Number.isFinite(p.value) && p.value > 0);
    const total = clean.reduce((sum, p) => sum + p.value, 0);
    if (total <= 0) return [];
    let at = 0;
    return clean.map((p) => {
      const sweep = (p.value / total) * 360;
      const arc = { name: p.name, value: p.value, start: at, end: at + sweep };
      at += sweep;
      return arc;
    });
  };

  // A closed donut segment between radii r0 and r1 around (cx, cy).
  const arcPath = (cx, cy, r0, r1, start, end) => {
    // 359.9, not 359.999: at two decimals the latter's end point equals its
    // start, and SVG draws an arc between identical points as nothing.
    const sweep = Math.min(end - start, 359.9);
    const rad = (deg) => (deg - 90) * Math.PI / 180;
    const pt = (r, deg) => [cx + r * Math.cos(rad(deg)), cy + r * Math.sin(rad(deg))];
    const large = sweep > 180 ? 1 : 0;
    const f = (n) => n.toFixed(2);
    const [ax, ay] = pt(r1, start);
    const [bx, by] = pt(r1, start + sweep);
    const [ix, iy] = pt(r0, start + sweep);
    const [jx, jy] = pt(r0, start);
    return 'M ' + f(ax) + ' ' + f(ay)
      + ' A ' + r1 + ' ' + r1 + ' 0 ' + large + ' 1 ' + f(bx) + ' ' + f(by)
      + ' L ' + f(ix) + ' ' + f(iy)
      + ' A ' + r0 + ' ' + r0 + ' 0 ' + large + ' 0 ' + f(jx) + ' ' + f(jy) + ' Z';
  };

  // Ease-out cubic from a to b; t is clamped to [0, 1].
  const tween = (a, b, t) => {
    const k = Math.min(1, Math.max(0, t));
    return a + (b - a) * (1 - Math.pow(1 - k, 3));
  };

  // Days between two ISO timestamps (dates alone are read as UTC midnight).
  const dayIndex = (iso, origin) => (Date.parse(iso) - Date.parse(origin)) / 86400000;

  return { scale, extent, linePath, arcs, arcPath, tween, dayIndex };
})();
"""
