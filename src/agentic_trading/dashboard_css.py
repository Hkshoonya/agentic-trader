"""Cockpit styles: tabs, the overview grid, the race, tiles and the ticker.

Added after the original console styles, so every existing card keeps its look
and only the new pieces (and the body font) are defined here.
"""

CSS = r"""
body{font:14px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
table,#stream,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;z-index:5}
.tabs{display:flex;gap:4px;margin-left:auto}
.tabs button{background:transparent;border:1px solid var(--line);color:var(--muted);padding:6px 14px;border-radius:999px;font:600 12px system-ui,sans-serif;cursor:pointer;transition:all .2s}
.tabs button:hover{color:var(--text);border-color:var(--accent)}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#04111f}
.tab{display:none}
.tab.on{display:block;animation:tabin .35s ease}
@keyframes tabin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.armbox{display:flex;align-items:center;gap:8px}
.netdot{width:9px;height:9px;border-radius:50%;background:var(--buy);animation:ping 2s infinite}
.netdot.lost{background:var(--warn);animation:none}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(53,208,127,.55)}70%{box-shadow:0 0 0 8px rgba(53,208,127,0)}100%{box-shadow:0 0 0 0 rgba(53,208,127,0)}}
/* The cockpit fits one 1440x900 screen: stories | race | tiles, ticker below. */
.cockpit{display:grid;grid-template-columns:1fr 2.2fr 1fr;gap:14px;padding:16px;height:calc(100vh - 150px);min-height:520px}
.stories,.tiles{display:flex;flex-direction:column;gap:12px;min-height:0}
.story{flex:1;background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--buy);border-radius:12px;padding:16px;display:flex;flex-direction:column;justify-content:center;min-height:0}
.story.money{border-left-color:var(--accent)}
.story.just{border-left-color:var(--warn)}
.story .k,.tile .k{color:var(--muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
.say{font-size:17px;font-weight:650;line-height:1.35}
.say.fade{animation:fadein .3s ease}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.race{display:flex;flex-direction:column;min-height:0;position:relative}
.racehead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
#race{flex:1;width:100%;min-height:0}
#race .zero{stroke:var(--line);stroke-dasharray:4 4}
#race .grid{fill:var(--muted);font-size:11px}
#race .line{fill:none;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
#race .line.bench{stroke-dasharray:6 5;stroke-width:2}
#race .hit{fill:none;stroke:transparent;stroke-width:14;cursor:pointer}
#race .end{font-size:12px;font-weight:700}
#race .lead{animation:beat 1.6s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
@keyframes beat{0%,100%{transform:scale(1)}50%{transform:scale(1.6)}}
#race .drawin{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.2s ease-out forwards}
@keyframes draw{to{stroke-dashoffset:0}}
#race .empty{fill:var(--muted);font-size:14px}
#race .dim{opacity:.25}
.tip{position:absolute;pointer-events:none;background:#0d1219;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;display:none;max-width:240px;z-index:3}
.tile{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;min-height:0;display:flex;flex-direction:column;justify-content:center}
.num{font-size:26px;font-weight:800;font-variant-numeric:tabular-nums}
.tile svg{width:100%;height:44px;display:block}
.tile.ring svg{height:90px}
.sweep{animation:sweep .9s ease-out;transform-origin:center;transform-box:fill-box}
@keyframes sweep{from{transform:rotate(-90deg) scale(.7);opacity:0}to{transform:none;opacity:1}}
.ticker{margin:0 16px 16px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;white-space:nowrap;padding:10px 0}
.tape{display:inline-block;padding-left:100%;animation:scroll 60s linear infinite}
.ticker:hover .tape{animation-play-state:paused}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-100%)}}
.item{display:inline-block;margin-right:36px;font-size:13px}
.item .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle;background:var(--muted)}
.item.fill .dot{background:var(--buy)}
.item.order .dot{background:var(--accent)}
.item.allocation .dot{background:var(--shadow)}
.item.overruled .dot{background:var(--warn)}
.item.error .dot{background:var(--sell)}
.item.new{animation:flashin 1.2s ease}
@keyframes flashin{0%{color:#fff;text-shadow:0 0 12px var(--accent)}100%{color:inherit;text-shadow:none}}
.members{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.member{background:#0f151e;border:1px solid var(--line);border-top:3px solid var(--muted);border-radius:10px;padding:12px}
.member h3{margin:0 0 6px;font-size:15px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.chip{background:#1b2431;border-radius:999px;padding:2px 9px;font-size:11px}
.progress{height:6px;background:#1b2431;border-radius:3px;overflow:hidden;margin:4px 0 8px}
.progress>div{height:100%;background:linear-gradient(90deg,var(--shadow),var(--accent));transition:width .6s ease}
@media(max-width:1100px){.cockpit{grid-template-columns:1fr;height:auto}.tabs{margin-left:0}}
"""
