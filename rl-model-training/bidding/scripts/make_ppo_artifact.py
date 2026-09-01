"""Build the PPO run-1 telemetry artifact from the live training log.

Re-runnable: point it at the log and it regenerates the page with whatever rows exist, so the
same artifact URL can be redeployed as the run advances.
"""

import io, json, re, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "artifacts" / (sys.argv[2] if len(sys.argv) > 2 else "run1.A_s0.log")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ppo_run1.html")

# -- parse ---------------------------------------------------------------------------
rows = []
for line in io.open(LOG, encoding="utf-8"):
    f = line.split()
    if len(f) >= 12 and f[0].isdigit():
        probe = float(f[12]) if len(f) >= 13 and re.match(r"^\d+\.\d+$", f[12]) else None
        rows.append(dict(
            it=int(f[0]), steps=int(f[1]), train=float(f[2]),
            ent=float(f[6]), kl=float(f[7]), clip=float(f[8].rstrip("%")),
            grad=float(f[9]), epochs=int(f[10]), aband=int(f[11]), probe=probe,
        ))

probes = [r for r in rows if r["probe"] is not None]
last = rows[-1]
first_probe, last_probe = probes[0], probes[-1]
probe_delta = (last_probe["probe"] - first_probe["probe"]) / first_probe["probe"]
train_delta = (last["train"] - rows[0]["train"]) / rows[0]["train"]

FACTS = dict(
    rows=rows,
    budget=296_000,
    gate=784.25,
    heuristic_train=671.13,
    heuristic_probe=648.41,
    cem_d=782.25,
    encoder="96ceb154f5fd",
    fabrication="f14a17eef7b1",
    snapshot=datetime.now().strftime("%Y-%m-%d %H:%M"),
)

DATA = json.dumps(FACTS, separators=(",", ":"))

HTML = """<title>PPO Run 1 Telemetry</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {
  color-scheme: light;
  --ground:      #f7f8fa;
  --surface:     #ffffff;
  --surface-2:   #eef1f5;
  --ink:         #111418;
  --ink-2:       #464e59;
  --ink-3:       #767f8c;
  --rule:        #dbe0e8;
  --rule-soft:   #e8ecf2;
  --train:       #2a78d6;
  --heldout:     #eb6834;
  --train-wash:  rgba(42,120,214,.09);
  --critical:    #c22f2f;
  --critical-bg: #fbeceb;
  --good:        #0a7d0a;
  --shadow:      0 1px 2px rgba(17,20,24,.05), 0 8px 24px -12px rgba(17,20,24,.14);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --ground:      #14171a;
    --surface:     #1b1f24;
    --surface-2:   #23282e;
    --ink:         #f2f4f7;
    --ink-2:       #b3bcc7;
    --ink-3:       #7d8794;
    --rule:        #2e343b;
    --rule-soft:   #262b31;
    --train:       #3987e5;
    --heldout:     #e0672f;
    --train-wash:  rgba(57,135,229,.14);
    --critical:    #e8706e;
    --critical-bg: #33201f;
    --good:        #46b846;
    --shadow:      0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.6);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ground:      #14171a;
  --surface:     #1b1f24;
  --surface-2:   #23282e;
  --ink:         #f2f4f7;
  --ink-2:       #b3bcc7;
  --ink-3:       #7d8794;
  --rule:        #2e343b;
  --rule-soft:   #262b31;
  --train:       #3987e5;
  --heldout:     #e0672f;
  --train-wash:  rgba(57,135,229,.14);
  --critical:    #e8706e;
  --critical-bg: #33201f;
  --good:        #46b846;
  --shadow:      0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.6);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 40px 24px 72px; }

/* ---- header strip ---- */
.eyebrow {
  font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif;
  font-weight: 600; font-size: 12px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--ink-3); margin: 0 0 10px;
}
h1 {
  font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif;
  font-weight: 700; font-size: clamp(30px, 5vw, 44px); line-height: 1.08;
  letter-spacing: -.015em; margin: 0 0 14px; text-wrap: balance;
}
.standfirst { font-size: 17px; color: var(--ink-2); max-width: 62ch; margin: 0 0 24px; }
.standfirst strong { color: var(--ink); font-weight: 600; }

.ident {
  display: flex; flex-wrap: wrap; gap: 0 28px;
  padding: 14px 18px; margin-bottom: 32px;
  background: var(--surface); border: 1px solid var(--rule); border-radius: 4px;
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12.5px;
}
.ident div { display: flex; gap: 8px; }
.ident dt { color: var(--ink-3); }
.ident dd { margin: 0; color: var(--ink); }

/* ---- verdict ---- */
.verdict {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 16px 18px; margin-bottom: 32px; border-radius: 4px;
  background: var(--critical-bg); border: 1px solid color-mix(in srgb, var(--critical) 35%, transparent);
}
.verdict svg { flex: none; margin-top: 2px; }
.verdict-label {
  font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif;
  font-weight: 700; font-size: 13px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--critical); display: block; margin-bottom: 3px;
}
.verdict p { margin: 0; font-size: 14.5px; color: var(--ink-2); }

/* ---- stat tiles ---- */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(178px, 1fr)); gap: 1px;
         background: var(--rule); border: 1px solid var(--rule); border-radius: 4px;
         overflow: hidden; margin-bottom: 40px; }
.tile { background: var(--surface); padding: 16px 18px 18px; }
.tile-k {
  font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif;
  font-size: 11.5px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase;
  color: var(--ink-3); margin-bottom: 6px;
}
.tile-v {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 27px; font-weight: 500; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; line-height: 1.1;
}
.tile-n { font-size: 12.5px; color: var(--ink-3); margin-top: 5px;
          font-family: "IBM Plex Mono", ui-monospace, monospace; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
       margin-right: 6px; vertical-align: 1px; }

/* ---- sections ---- */
section { margin-bottom: 40px; }
h2 {
  font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif;
  font-size: 20px; font-weight: 700; letter-spacing: -.005em; margin: 0 0 6px;
}
.sub { color: var(--ink-2); font-size: 14.5px; margin: 0 0 18px; max-width: 68ch; }

.panel { background: var(--surface); border: 1px solid var(--rule); border-radius: 4px;
         padding: 20px 20px 12px; box-shadow: var(--shadow); }
.legend { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 14px;
          font-size: 13px; color: var(--ink-2); }
.legend span { display: flex; align-items: center; gap: 7px; }
.swatch { width: 22px; height: 3px; border-radius: 2px; flex: none; }
.swatch.dash { height: 0; border-top: 2px dashed currentColor; }

.chart { position: relative; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.grid line { stroke: var(--rule-soft); stroke-width: 1; }
.axis text { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px;
             fill: var(--ink-3); font-variant-numeric: tabular-nums; }
.axis-title { font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif;
              font-size: 11px; font-weight: 600; letter-spacing: .07em;
              text-transform: uppercase; fill: var(--ink-3); }
.endlabel { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px;
            font-weight: 500; font-variant-numeric: tabular-nums; }
.reflabel { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 10.5px;
            fill: var(--ink-3); }

.tip { position: absolute; pointer-events: none; opacity: 0; transition: opacity .1s;
       background: var(--surface); border: 1px solid var(--rule); border-radius: 4px;
       box-shadow: var(--shadow); padding: 9px 11px; font-size: 12.5px;
       font-family: "IBM Plex Mono", ui-monospace, monospace; white-space: nowrap;
       font-variant-numeric: tabular-nums; z-index: 5; }
.tip.on { opacity: 1; }
.tip b { font-weight: 600; }
.tip .r { color: var(--ink-3); }

.smalls { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }

/* ---- table ---- */
.tablewrap { overflow-x: auto; border: 1px solid var(--rule); border-radius: 4px;
             background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-family: "IBM Plex Mono", ui-monospace, monospace;
        font-size: 12.5px; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 14px; text-align: right; white-space: nowrap; }
th { font-family: "IBM Plex Sans Condensed", ui-sans-serif, sans-serif; font-size: 11px;
     font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: var(--ink-3);
     border-bottom: 1px solid var(--rule); position: sticky; top: 0; background: var(--surface); }
td { border-bottom: 1px solid var(--rule-soft); color: var(--ink-2); }
tbody tr:hover td { background: var(--surface-2); }
td:first-child, th:first-child { text-align: left; color: var(--ink); }
.mark-probe { color: var(--heldout); font-weight: 500; }

.note { font-size: 13.5px; color: var(--ink-3); max-width: 70ch; }
.note code { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12.5px;
             background: var(--surface-2); padding: 1px 5px; border-radius: 3px; color: var(--ink-2); }
footer { border-top: 1px solid var(--rule); padding-top: 20px; margin-top: 48px;
         font-size: 13px; color: var(--ink-3); max-width: 74ch; }
a { color: var(--train); }
:focus-visible { outline: 2px solid var(--train); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<div class="wrap">
  <p class="eyebrow">PPO_EXPERIMENT_PLAN · Run 1 · Experiment A · PPO seed 0</p>
  <h1>Training climbs. Held-out falls.</h1>
  <p class="standfirst">
    PPO on the ICU-bed auction simulator, one agent learning against two frozen heuristics.
    The training return has more than <strong>doubled</strong>. On worlds it has never seen, the
    same policy has lost <strong id="s-drop">32%</strong> over the same span — and crossed below
    the heuristic it is supposed to beat.
  </p>

  <dl class="ident">
    <div><dt>encoder</dt><dd id="i-enc"></dd></div>
    <div><dt>fabrication</dt><dd id="i-fab"></dd></div>
    <div><dt>params</dt><dd>207</dd></div>
    <div><dt>train seeds</dt><dd>11–18 × 4 shifts</dd></div>
    <div><dt>snapshot</dt><dd id="i-snap"></dd></div>
  </dl>

  <div class="verdict" role="note">
    <svg width="17" height="17" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 1.6 15 14H1L8 1.6Z" fill="none" stroke="currentColor" stroke-width="1.6"
            stroke-linejoin="round" style="color:var(--critical)"/>
      <path d="M8 6v3.6M8 11.4v.9" stroke="currentColor" stroke-width="1.6"
            stroke-linecap="round" style="color:var(--critical)"/>
    </svg>
    <div>
      <span class="verdict-label">Overfitting, not undertraining</span>
      <p>
        More budget makes this worse, not better — so §4's <em>inconclusive — budget-limited</em>
        clause does not apply. 8 seeds × 4 shifts is only 32 distinct worlds, and a 2,048-step
        rollout replays them about 5×, so each world is seen roughly 700 times across the run.
        The remedy is world diversity, not steps.
      </p>
    </div>
  </div>

  <div class="tiles" id="tiles"></div>

  <section>
    <h2>Discounted return vs simulator interaction</h2>
    <p class="sub">
      Both series are mean ER discounted return per shift-episode, in the same units, so they
      share one axis. Each carries its own dashed baseline: the heuristic measured on that
      series' own seeds.
    </p>
    <div class="panel">
      <div class="legend">
        <span><i class="swatch" style="background:var(--train)"></i> Training seeds 11–18, sampled</span>
        <span><i class="swatch" style="background:var(--heldout)"></i> Held-out probe 101–110, deterministic</span>
        <span style="color:var(--ink-3)"><i class="swatch dash"></i> Heuristic on the same seeds</span>
      </div>
      <div class="chart" id="main"><div class="tip" id="tip"></div></div>
    </div>
  </section>

  <section>
    <h2>What the optimiser was doing</h2>
    <p class="sub">
      The two diagnostics that say whether the run was still learning. Entropy falling means the
      policy is committing; approximate KL collapsing toward zero means the updates have gone
      quiet — together they say this policy converged rather than ran out of budget.
    </p>
    <div class="smalls">
      <div class="panel">
        <div class="legend"><span>Policy entropy — categorical head, nats</span></div>
        <div class="chart" id="ent"></div>
      </div>
      <div class="panel">
        <div class="legend"><span>Approximate KL per update, against the 0.02 early stop</span></div>
        <div class="chart" id="kl"></div>
      </div>
    </div>
  </section>

  <section>
    <h2>Every iteration</h2>
    <p class="sub">
      The same numbers as the chart, for reading exactly. Rows carrying a held-out probe are
      marked; <code>epochs</code> below 5 means the KL early stop fired.
    </p>
    <div class="tablewrap"><table>
      <thead><tr>
        <th scope="col">Iter</th><th scope="col">Env-steps</th><th scope="col">Train return</th>
        <th scope="col">Held-out</th><th scope="col">Entropy</th><th scope="col">Approx KL</th>
        <th scope="col">Clip</th><th scope="col">‖grad‖</th><th scope="col">Epochs</th>
        <th scope="col">Abandon</th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table></div>
  </section>

  <footer>
    <p>
      Whatever these curves say, they are statements about a simulator whose outcome model is
      invented and whose reward table has never been fitted. Per the plan's §10, this answers
      which policy paces better — never which policy saves more patients. Abandonments stayed at
      zero on every iteration, which is Experiment A's mask holding.
    </p>
  </footer>
</div>

<script>
const D = __DATA__;
const R = D.rows, probes = R.filter(r => r.probe !== null);
const last = R[R.length - 1], fp = probes[0], lp = probes[probes.length - 1];
const fmt = (n, d = 2) => n.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = n => (n >= 0 ? "+" : "") + (n * 100).toFixed(1) + "%";

document.getElementById("i-enc").textContent = D.encoder;
document.getElementById("i-fab").textContent = D.fabrication;
document.getElementById("i-snap").textContent = D.snapshot;
const drop = (lp.probe - fp.probe) / fp.probe;
document.getElementById("s-drop").textContent = Math.abs(drop * 100).toFixed(0) + "%";

/* ---- stat tiles ---- */
const tiles = [
  {k: "Iteration", v: last.it + " / ~144", n: fmt(last.steps / D.budget * 100, 0) + "% of the 296k budget"},
  {k: "Training return", v: fmt(last.train), n: pct((last.train - R[0].train) / R[0].train) + " from 59.93", dot: "var(--train)"},
  {k: "Held-out probe", v: fmt(lp.probe), n: pct(drop) + " from " + fmt(fp.probe), dot: "var(--heldout)"},
  {k: "Heuristic to beat", v: fmt(D.heuristic_probe), n: "on the same probe seeds"},
  {k: "Abandonments", v: R.reduce((a, r) => a + r.aband, 0), n: "hard criterion, all iterations"},
];
document.getElementById("tiles").innerHTML = tiles.map(t =>
  `<div class="tile"><div class="tile-k">${t.dot
      ? `<i class="dot" style="background:${t.dot}"></i>` : ""}${t.k}</div>
   <div class="tile-v">${t.v}</div><div class="tile-n">${t.n}</div></div>`).join("");

/* ---- chart engine ---- */
const NS = "http://www.w3.org/2000/svg";
function el(n, a) { const e = document.createElementNS(NS, n);
  for (const k in a) e.setAttribute(k, a[k]); return e; }

function chart(host, opt) {
  const W = 1000, H = opt.h || 380, M = opt.m || {t: 16, r: 74, b: 42, l: 56};
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img",
                         "aria-label": opt.label});
  const x = v => M.l + (v - opt.x[0]) / (opt.x[1] - opt.x[0]) * iw;
  const y = v => M.t + ih - (v - opt.y[0]) / (opt.y[1] - opt.y[0]) * ih;

  const g = el("g", {class: "grid"});
  opt.yt.forEach(t => g.appendChild(el("line", {x1: M.l, x2: M.l + iw, y1: y(t), y2: y(t)})));
  svg.appendChild(g);

  if (opt.beyond !== undefined) {
    svg.appendChild(el("rect", {x: x(opt.beyond), y: M.t, width: Math.max(0, M.l + iw - x(opt.beyond)),
      height: ih, fill: "var(--surface-2)", opacity: ".55"}));
    const t = el("text", {x: x(opt.beyond) + 8, y: M.t + 14, class: "reflabel"});
    t.textContent = "budget not yet spent"; svg.appendChild(t);
  }

  const ax = el("g", {class: "axis"});
  opt.yt.forEach(t => { const e = el("text", {x: M.l - 10, y: y(t) + 4, "text-anchor": "end"});
    e.textContent = opt.yfmt ? opt.yfmt(t) : t; ax.appendChild(e); });
  opt.xt.forEach(t => { const e = el("text", {x: x(t), y: M.t + ih + 20, "text-anchor": "middle"});
    e.textContent = opt.xfmt(t); ax.appendChild(e); });
  const xa = el("text", {x: M.l, y: H - 4, class: "axis-title"});
  xa.textContent = opt.xtitle; ax.appendChild(xa);
  svg.appendChild(ax);
  svg.appendChild(el("line", {x1: M.l, x2: M.l + iw, y1: M.t + ih, y2: M.t + ih,
    stroke: "var(--rule)", "stroke-width": 1}));

  (opt.refs || []).forEach(r => {
    svg.appendChild(el("line", {x1: M.l, x2: M.l + iw, y1: y(r.v), y2: y(r.v),
      stroke: r.c, "stroke-width": 2, "stroke-dasharray": "6 5", opacity: ".75"}));
    const t = el("text", {x: M.l + iw + 8, y: y(r.v) + 4 + (r.dy || 0), class: "reflabel"});
    t.textContent = r.t; svg.appendChild(t);
  });

  (opt.series || []).forEach(s => {
    const pts = s.data.filter(d => d.v !== null);
    if (!pts.length) return;
    if (s.fill) {
      const d = "M" + x(pts[0].t) + "," + y(opt.y[0]) + pts.map(p => "L" + x(p.t) + "," + y(p.v)).join("")
        + "L" + x(pts[pts.length - 1].t) + "," + y(opt.y[0]) + "Z";
      svg.appendChild(el("path", {d, fill: s.fill, stroke: "none"}));
    }
    svg.appendChild(el("path", {d: "M" + pts.map(p => x(p.t) + "," + y(p.v)).join("L"),
      fill: "none", stroke: s.c, "stroke-width": s.w || 2, "stroke-linejoin": "round",
      "stroke-linecap": "round"}));
    if (s.dots) pts.forEach(p => {
      svg.appendChild(el("circle", {cx: x(p.t), cy: y(p.v), r: 4.5, fill: s.c,
        stroke: "var(--surface)", "stroke-width": 2}));
    });
    const e = pts[pts.length - 1];
    svg.appendChild(el("circle", {cx: x(e.t), cy: y(e.v), r: 5.5, fill: s.c,
      stroke: "var(--surface)", "stroke-width": 2}));
    if (s.endlabel) {
      const t = el("text", {x: x(e.t) + 11, y: y(e.v) + 4, class: "endlabel", fill: s.c});
      t.textContent = fmt(e.v, s.dp === undefined ? 0 : s.dp); svg.appendChild(t);
    }
  });

  host.insertBefore(svg, host.firstChild);
  return {svg, x, y, M, iw, ih, W, H};
}

/* ---- main chart ---- */
const XMAX = D.budget;
const c = chart(document.getElementById("main"), {
  label: "Training return rises from 60 to 523 while the held-out probe falls from 717 to " + fmt(lp.probe, 0),
  x: [0, XMAX], y: [0, 840], beyond: last.steps, m: {t: 16, r: 118, b: 42, l: 56},
  yt: [0, 200, 400, 600, 800], xt: [0, 74000, 148000, 222000, 296000],
  xfmt: t => (t / 1000) + "k", xtitle: "ER env-steps",
  refs: [
    {v: D.gate, c: "var(--ink-3)", t: "gate " + D.gate},
    {v: D.heuristic_train, c: "var(--train)", t: "heuristic " + fmt(D.heuristic_train, 0), dy: -5},
    {v: D.heuristic_probe, c: "var(--heldout)", t: "heuristic " + fmt(D.heuristic_probe, 0), dy: 12},
  ],
  series: [
    {c: "var(--train)", fill: "var(--train-wash)", endlabel: true,
     data: R.map(r => ({t: r.steps, v: r.train}))},
    {c: "var(--heldout)", w: 2.5, dots: true, endlabel: true,
     data: probes.map(r => ({t: r.steps, v: r.probe}))},
  ],
});

/* ---- crosshair + tooltip ---- */
const tip = document.getElementById("tip");
const cross = el("line", {y1: c.M.t, y2: c.M.t + c.ih, stroke: "var(--ink-3)",
  "stroke-width": 1, "stroke-dasharray": "3 3", opacity: "0"});
c.svg.appendChild(cross);
const hit = el("rect", {x: 0, y: c.M.t, width: c.W, height: c.ih, fill: "transparent",
  style: "cursor:crosshair"});
c.svg.appendChild(hit);

function nearest(steps) {
  let best = R[0];
  for (const r of R) if (Math.abs(r.steps - steps) < Math.abs(best.steps - steps)) best = r;
  return best;
}
function move(ev) {
  const b = c.svg.getBoundingClientRect();
  const sx = (ev.clientX - b.left) / b.width * c.W;
  const steps = (sx - c.M.l) / c.iw * (XMAX - 0);
  const r = nearest(steps);
  cross.setAttribute("x1", c.x(r.steps)); cross.setAttribute("x2", c.x(r.steps));
  cross.setAttribute("opacity", ".7");
  const pr = r.probe !== null ? `<br><span class="r">held-out</span> <b>${fmt(r.probe)}</b>` : "";
  tip.innerHTML = `<b>iter ${r.it}</b> · ${(r.steps / 1000).toFixed(1)}k steps<br>`
    + `<span class="r">training</span> <b>${fmt(r.train)}</b>${pr}`
    + `<br><span class="r">entropy</span> ${fmt(r.ent, 3)} <span class="r">· KL</span> ${fmt(r.kl, 4)}`;
  tip.classList.add("on");
  const px = c.x(r.steps) / c.W * b.width;
  tip.style.left = Math.min(b.width - 200, Math.max(0, px + 14)) + "px";
  tip.style.top = "18px";
}
hit.addEventListener("pointermove", move);
hit.addEventListener("pointerleave", () => { tip.classList.remove("on"); cross.setAttribute("opacity", "0"); });

/* ---- small multiples ---- */
chart(document.getElementById("ent"), {
  label: "Policy entropy falls from 1.29 to " + fmt(last.ent, 2) + " nats",
  h: 250, m: {t: 14, r: 52, b: 40, l: 44},
  x: [0, last.steps], y: [0, 1.45], yt: [0, 0.5, 1.0, 1.45],
  xt: [0, Math.round(last.steps / 2), last.steps],
  yfmt: t => t.toFixed(1), xfmt: t => (t / 1000).toFixed(0) + "k", xtitle: "env-steps",
  series: [{c: "var(--train)", fill: "var(--train-wash)", endlabel: true, dp: 2,
            data: R.map(r => ({t: r.steps, v: r.ent}))}],
});

chart(document.getElementById("kl"), {
  label: "Approximate KL collapses toward zero, below the 0.02 early-stop threshold",
  h: 250, m: {t: 14, r: 52, b: 40, l: 52},
  x: [0, last.steps], y: [-0.03, 0.05], yt: [-0.02, 0, 0.02, 0.04],
  xt: [0, Math.round(last.steps / 2), last.steps],
  yfmt: t => t.toFixed(2), xfmt: t => (t / 1000).toFixed(0) + "k", xtitle: "env-steps",
  refs: [{v: 0.02, c: "var(--ink-3)", t: "stop"}],
  series: [{c: "var(--heldout)", endlabel: true, dp: 3,
            data: R.map(r => ({t: r.steps, v: r.kl}))}],
});

/* ---- table ---- */
document.getElementById("tbody").innerHTML = R.map(r => `<tr>
  <td>${r.it}</td><td>${r.steps.toLocaleString("en-US")}</td><td>${fmt(r.train)}</td>
  <td class="${r.probe !== null ? "mark-probe" : ""}">${r.probe !== null ? fmt(r.probe) : "·"}</td>
  <td>${fmt(r.ent, 3)}</td><td>${fmt(r.kl, 4)}</td><td>${r.clip.toFixed(1)}%</td>
  <td>${fmt(r.grad, 3)}</td><td>${r.epochs}</td><td>${r.aband}</td></tr>`).join("");
</script>
"""

OUT.write_text(HTML.replace("__DATA__", DATA), encoding="utf-8")
print(f"wrote {OUT}  ({len(rows)} iterations, {len(probes)} probes, latest it={last['it']})")
