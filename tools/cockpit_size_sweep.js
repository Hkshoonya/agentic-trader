// Cockpit screen-size regression check. Run it with the Playwright MCP tool
// browser_run_code_unsafe (filename: tools/cockpit_size_sweep.js) against the live
// dashboard on 127.0.0.1:8787; it returns {pass, failures, results}.
async (page) => {
  // Every size must: never scroll sideways; give the race at least 250px of
  // height; on desktops (>= 1280 wide) fit one screen with no empty band
  // under the ticker. Type must grow on big screens.
  const sizes = [[3840, 2160], [2560, 1440], [1920, 1080], [1536, 864], [1440, 900],
                 [1366, 768], [1280, 720], [1024, 768], [768, 1024], [390, 844]];
  const results = {};
  const failures = [];
  for (const [w, h] of sizes) {
    await page.setViewportSize({ width: w, height: h });
    await page.goto('http://127.0.0.1:8787/');
    await page.evaluate(() => { try { localStorage.setItem('tab', 'overview'); } catch (e) {} });
    await page.reload();
    await page.waitForTimeout(2200);
    const m = await page.evaluate(() => {
      const box = (s) => document.querySelector(s).getBoundingClientRect();
      return {
        vOver: document.scrollingElement.scrollHeight - innerHeight,
        hOver: document.scrollingElement.scrollWidth - document.documentElement.clientWidth,
        gap: Math.round(innerHeight - box('.ticker').bottom),
        race: Math.round(box('#race').height),
        say: parseFloat(getComputedStyle(document.getElementById('say-now')).fontSize),
        tab: parseFloat(getComputedStyle(document.querySelector('#tabs button')).fontSize),
        plot: (() => { const l = [...document.querySelectorAll('#race path.line')].map(p => p.getBBox()); const svg = document.getElementById('race');
          return l.length ? Math.round(Math.max(...l.map(b => b.x + b.width)) - Math.min(...l.map(b => b.x))) / svg.clientWidth : 0; })(),
        label: parseFloat((document.querySelector('#race .end') || {}).getAttribute
          ? document.querySelector('#race .end').getAttribute('font-size') || getComputedStyle(document.querySelector('#race .end')).fontSize : 0),
      };
    });
    const key = w + 'x' + h;
    results[key] = m;
    if (m.hOver > 0) failures.push(key + ' scrolls sideways by ' + m.hOver);
    if (m.race < 250) failures.push(key + ' race only ' + m.race + 'px tall');
    if (m.plot < 0.4) failures.push(key + ' race lines use only ' + Math.round(m.plot * 100) + '% of the chart width');
    if (w >= 1280 && m.vOver > 2) failures.push(key + ' overflows by ' + m.vOver);
    if (w >= 1280 && m.gap > 30) failures.push(key + ' leaves ' + m.gap + 'px empty under the ticker');
  }
  if (!(results['2560x1440'].say > results['1280x720'].say * 1.3)) {
    failures.push('story text does not grow: ' + results['1280x720'].say + ' -> ' + results['2560x1440'].say);
  }
  if (!(results['2560x1440'].label > results['1280x720'].label * 1.3)) {
    failures.push('race labels do not grow: ' + results['1280x720'].label + ' -> ' + results['2560x1440'].label);
  }
  if (!(results['2560x1440'].tab > results['1280x720'].tab * 1.25)) {
    failures.push('header tabs do not grow: ' + results['1280x720'].tab + ' -> ' + results['2560x1440'].tab);
  }
  return { pass: failures.length === 0, failures, results };
}
