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
/* The header scales with the screen too; its sizes were fixed pixels. */
header h1{font-size:clamp(15px,.5vw + 8px,22px)}
header .badge{font-size:clamp(11px,.4vw + 6px,17px)}
#generated{font-size:clamp(12px,.4vw + 6px,16px)}
.tabs button{font-size:clamp(12px,.45vw + 6px,18px);padding:.5em 1.15em}
#t-money-legend{font-size:clamp(11px,.45vw + 5px,17px)}
.tab{display:none}
.tab.on{display:block;animation:tabin .35s ease}
@keyframes tabin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.armbox{display:flex;align-items:center;gap:8px}
.netdot{width:9px;height:9px;border-radius:50%;background:var(--buy);animation:ping 2s infinite}
.netdot.warn{background:var(--warn);animation:none}
.netdot.bad{background:var(--sell);animation:pingbad 1.2s infinite}
.netdot.unknown{background:var(--muted);animation:none}
@keyframes pingbad{0%{box-shadow:0 0 0 0 rgba(255,95,109,.6)}70%{box-shadow:0 0 0 8px rgba(255,95,109,0)}100%{box-shadow:0 0 0 0 rgba(255,95,109,0)}}
/* Lost connection is drawn over any health colour: a hollow ring. */
.netdot.lost{background:transparent;box-shadow:inset 0 0 0 2px var(--muted);animation:none}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(53,208,127,.55)}70%{box-shadow:0 0 0 8px rgba(53,208,127,0)}100%{box-shadow:0 0 0 0 rgba(53,208,127,0)}}
/* The overview fills the screen below the header, whatever its size: --head is
   the header's measured height (set by the cockpit script), never a guess. */
#tab-overview.on{display:flex;flex-direction:column;height:calc(100vh - var(--head, 60px));height:calc(100dvh - var(--head, 60px));min-height:560px}
.cockpit{display:grid;grid-template-columns:1fr 2.2fr 1fr;gap:clamp(10px,1vw,22px);padding:clamp(10px,1vw,22px);flex:1 1 auto;min-height:0}
.stories,.tiles{display:flex;flex-direction:column;gap:12px;min-height:0}
.story{flex:1;background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--buy);border-radius:12px;padding:clamp(12px,1vw,26px);display:flex;flex-direction:column;justify-content:center;min-height:0}
.story.money{border-left-color:var(--accent)}
.story.just{border-left-color:var(--warn)}
.story .k,.tile .k{color:var(--muted);font-size:clamp(10px,.45vw + 4px,15px);letter-spacing:.12em;text-transform:uppercase;margin-bottom:6px}
.say{font-size:clamp(15px,.85vw + 6px,30px);font-weight:650;line-height:1.35}
.say.fade{animation:fadein .3s ease}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.race{display:flex;flex-direction:column;min-height:0;position:relative}
.racehead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.race .racehead h2,.race .racehead .sub{font-size:clamp(11px,.5vw + 5px,16px)}
#race{flex:1;width:100%;min-height:0}
#race .zero{stroke:var(--line);stroke-dasharray:4 4}
#race .grid{fill:var(--muted)}
#race .line{fill:none;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
#race .line.bench{stroke-dasharray:6 5;stroke-width:2}
#race .hit{fill:none;stroke:transparent;stroke-width:14;cursor:pointer}
#race .end{font-weight:700}
#race .lead{animation:beat 1.6s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
@keyframes beat{0%,100%{transform:scale(1)}50%{transform:scale(1.6)}}
#race .drawin{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.2s ease-out forwards}
@keyframes draw{to{stroke-dashoffset:0}}
#race .empty{fill:var(--muted);font-size:clamp(13px,.7vw + 4px,22px)}
#race .dim{opacity:.25}
.tip{position:absolute;pointer-events:none;background:#0d1219;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:clamp(12px,.6vw + 4px,18px);display:none;max-width:clamp(240px,18vw,420px);z-index:3}
.tile{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:clamp(10px,.9vw,24px);min-height:0;display:flex;flex-direction:column;justify-content:center}
.num{font-size:clamp(22px,1.3vw + 8px,48px);font-weight:800;font-variant-numeric:tabular-nums}
.tile svg{width:100%;height:clamp(36px,5vh,110px);display:block}
.tile.ring svg{height:clamp(64px,11vh,220px)}
.sweep{animation:sweep .9s ease-out;transform-origin:center;transform-box:fill-box}
@keyframes sweep{from{transform:rotate(-90deg) scale(.7);opacity:0}to{transform:none;opacity:1}}
.ticker{margin:0 clamp(10px,1vw,22px) clamp(10px,1vw,22px);background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;white-space:nowrap;padding:10px 0}
.tape{display:inline-block;padding-left:100%;animation:scroll 60s linear infinite}
.ticker:hover .tape{animation-play-state:paused}
@keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-100%)}}
.item{display:inline-block;margin-right:2.5em;font-size:clamp(12px,.75vw + 3px,20px)}
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
.venues{display:grid;gap:10px}
.vrow{display:flex;gap:14px;flex-wrap:wrap;align-items:center;padding:6px 0;border-bottom:1px dashed #1a2330}
.vrow b{min-width:130px}
.vstat{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--muted);margin-right:6px}
.vstat.live,.vstat.ok{background:var(--buy)}
.vstat.closed{background:var(--shadow)}
.vstat.stale,.vstat.reconnecting,.vstat.starting,.vstat.error{background:var(--warn)}
.vstat.auth_failed{background:var(--sell)}
/* Stacked: the page scrolls, so the race needs a height of its own (it was a
   150px strip), and the tiles pair up instead of stretching full width. */
@media(max-width:1100px){#tab-overview.on{display:block;height:auto;min-height:0}.cockpit{grid-template-columns:1fr}.tiles{display:grid;grid-template-columns:1fr 1fr}#race{flex:none;height:clamp(260px,55vw,460px)}.tabs{margin-left:0}}
@media(max-width:700px){#generated,#pulse{display:none}}
"""
