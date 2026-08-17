"""Generate the feedback dashboard (single-file HTML, no external deps, follows system dark mode).

The dashboard answers exactly three questions:
  1. Did I do enough today?              -> leading-indicator tiles + 56-day application heat strip
  2. Where is my funnel stuck?           -> funnel
  3. Which channel/resume version works? -> grouped reply rates + weekly cohorts
"""

from __future__ import annotations

import json

from .config import DASHBOARD_PATH
from .track import stats

# Colors come from the dataviz reference palette; the funnel uses a blue ordinal
# ramp that passes the validator in both light and dark mode.
TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobpipe dashboard</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb; --plane: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --f1:#86b6ef; --f2:#5598e7; --f3:#2a78d6; --f4:#1c5cab; --f5:#104281;
    --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
    --success-text:#006300;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19; --plane: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --series-1: #3987e5;
      --f1:#cde2fb; --f2:#9ec5f4; --f3:#6da7ec; --f4:#3987e5; --f5:#256abf;
      --success-text:#0ca30c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 24px 64px; background: var(--plane); color: var(--text-primary);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 28px; }
  h2 { font-size: 14px; font-weight: 600; color: var(--text-secondary); margin: 0 0 14px;
       text-transform: uppercase; letter-spacing: 0.06em; }
  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
          padding: 20px; margin-bottom: 20px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr)); gap: 12px;
           margin-bottom: 20px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
          padding: 16px 18px; }
  .tile .k { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .tile .v { font-size: 30px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
  .tile .n { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
  .ok { color: var(--success-text); }

  /* 56-day application bar chart — single series, the heading is the legend */
  .bars { display: flex; align-items: flex-end; gap: 2px; height: 120px;
          border-bottom: 1px solid var(--baseline); padding-bottom: 0; }
  .bar { flex: 1; min-width: 3px; background: var(--series-1); border-radius: 4px 4px 0 0;
         min-height: 2px; opacity: .9; cursor: default; transition: opacity .12s; }
  .bar:hover { opacity: 1; }
  .bar.zero { background: var(--grid); }
  /* Absolute positioning — otherwise 56 flex cells are each too narrow and clip the dates */
  .xaxis { position: relative; height: 16px; margin-top: 6px; }
  .xaxis span { position: absolute; top: 0; font-size: 10px; color: var(--muted);
                white-space: nowrap; transform: translateX(-50%); }

  /* Funnel — blue ordinal ramp, each stage labeled with count and conversion rate */
  .fstage { display: grid; grid-template-columns: 74px 1fr 130px; align-items: center;
            gap: 12px; margin-bottom: 8px; }
  .fstage .lbl { font-size: 13px; color: var(--text-secondary); text-align: right; }
  .ftrack { background: var(--grid); border-radius: 4px; height: 26px; overflow: hidden; }
  .ffill { height: 100%; border-radius: 4px; min-width: 3px; }
  .fstage .num { font-size: 13px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-weight: 600; color: var(--muted); font-size: 11px;
       text-transform: uppercase; letter-spacing: .05em; padding: 0 10px 8px 0;
       border-bottom: 1px solid var(--grid); }
  td { padding: 9px 10px 9px 0; border-bottom: 1px solid var(--grid);
       font-variant-numeric: tabular-nums; color: var(--text-secondary); }
  td.name { color: var(--text-primary); font-variant-numeric: normal; }
  tr:last-child td { border-bottom: none; }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 720px) { .two { grid-template-columns: 1fr; } .fstage { grid-template-columns: 60px 1fr 96px; } }

  .badge { display: inline-flex; align-items: center; gap: 5px; font-size: 11px;
           padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border);
           color: var(--text-secondary); white-space: nowrap; }
  .badge::before { content: ""; width: 7px; height: 7px; border-radius: 50%;
                   background: var(--muted); flex: none; }
  .badge.good::before { background: var(--good); }
  .badge.warning::before { background: var(--warning); }
  .badge.serious::before { background: var(--serious); }
  .badge.critical::before { background: var(--critical); }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
  a { color: var(--series-1); text-decoration: none; }
  a:hover { text-decoration: underline; }

  #tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
         background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--border);
         border-radius: 7px; padding: 7px 10px; font-size: 12px; z-index: 9;
         box-shadow: 0 4px 16px rgba(0,0,0,.14); white-space: nowrap; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Application dashboard</h1>
  <div class="sub">Generated __GENERATED__ · data from data/pipeline.db</div>

  <div class="tiles" id="tiles"></div>

  <div class="card">
    <h2>Applications per day (last 8 weeks)</h2>
    <div class="bars" id="bars"></div>
    <div class="xaxis" id="xaxis"></div>
  </div>

  <div class="card">
    <h2>Funnel</h2>
    <div id="funnel"></div>
  </div>

  <div class="two">
    <div class="card">
      <h2>Reply rate by channel</h2>
      <div id="chan"></div>
    </div>
    <div class="card">
      <h2>Reply rate by resume version</h2>
      <div id="resume"></div>
    </div>
  </div>

  <div class="two">
    <div class="card">
      <h2>Weekly cohorts</h2>
      <div id="cohort"></div>
    </div>
    <div class="card">
      <h2>Due for follow-up</h2>
      <div id="follow"></div>
    </div>
  </div>

  <div class="card">
    <h2>Active pipeline</h2>
    <div id="pipe"></div>
  </div>
</div>
<div id="tip"></div>

<script>
const D = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const tip = $("tip");
function bindTip(el, html) {
  el.addEventListener("mousemove", e => {
    tip.innerHTML = html; tip.style.opacity = 1;
    tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 10) + "px";
    tip.style.top  = (e.clientY - tip.offsetHeight - 12) + "px";
  });
  el.addEventListener("mouseleave", () => tip.style.opacity = 0);
}

/* ── tiles ─────────────────────────────────────────── */
const t = D.today, w = D.week;
const decided = D.by_channel.reduce((a, c) => a + c.sent - c.pending, 0);
const replied = D.by_channel.reduce((a, c) => a + c.replied, 0);
const overall = decided ? (100 * replied / decided).toFixed(1) + "%" : "—";
$("tiles").innerHTML = [
  ["Applied today", t.apply, `this week ${w.apply || 0}`],
  ["Streak", D.streak + " days", D.streak >= 3 ? "keep it going" : "getting started"],
  ["Referral asks today", t.referral_ask, `this week ${w.referral_ask || 0}`],
  ["Overall reply rate", overall, `${replied}/${decided} decided`],
  ["Open inventory", D.inventory.keep, `${D.inventory.fresh} not yet reviewed`],
].map(([k, v, n]) => `<div class="tile"><div class="k">${esc(k)}</div>
  <div class="v">${esc(v)}</div><div class="n">${esc(n)}</div></div>`).join("");

/* ── daily applications bar chart ───────────────────── */
const max = Math.max(1, ...D.daily.map(d => d.n));
$("bars").innerHTML = D.daily.map(d =>
  `<div class="bar${d.n ? "" : " zero"}" data-d="${d.day}" data-n="${d.n}"
        style="height:${d.n ? Math.max(6, 100 * d.n / max) : 2}%"></div>`).join("");
[...$("bars").children].forEach(el =>
  bindTip(el, `<b>${el.dataset.d}</b> · ${el.dataset.n} applications`));
$("xaxis").innerHTML = D.daily.map((d, i) => i % 14 === 0
  ? `<span style="left:${100 * (i + 0.5) / D.daily.length}%">${d.day.slice(5)}</span>` : "")
  .join("");

/* ── funnel ────────────────────────────────────────── */
const NAME = {applied:"Applied", screen:"HR screen", tech:"Tech interview", onsite:"Onsite", offer:"Offer"};
const SHADE = ["var(--f1)","var(--f2)","var(--f3)","var(--f4)","var(--f5)"];
// Note: do not name this variable `top` — that is window.top, and const would throw a SyntaxError
const funnelTop = Math.max(1, D.funnel[0].n);
$("funnel").innerHTML = D.funnel.map((s, i) => {
  const prev = i ? D.funnel[i-1].n : null;
  const conv = prev ? (100 * s.n / prev).toFixed(0) + "%" : "";
  return `<div class="fstage">
    <div class="lbl">${NAME[s.stage]}</div>
    <div class="ftrack"><div class="ffill" data-s="${NAME[s.stage]}" data-n="${s.n}"
      style="width:${Math.max(0.5, 100 * s.n / funnelTop)}%;background:${SHADE[i]}"></div></div>
    <div class="num">${s.n}${conv ? ` <span style="color:var(--muted)">· ${conv} of previous</span>` : ""}</div>
  </div>`;
}).join("");
document.querySelectorAll(".ffill").forEach(el =>
  bindTip(el, `<b>${el.dataset.s}</b> · ${el.dataset.n}`));

/* ── tables ────────────────────────────────────────── */
function rateTable(rows, label) {
  if (!rows.length) return `<div class="empty">No data yet — apply and it will appear.</div>`;
  return `<table><thead><tr><th>${label}</th><th>Sent</th><th>Replied</th>
    <th>Pending</th><th>Reply rate</th></tr></thead><tbody>` + rows.map(r =>
    `<tr><td class="name">${esc(r.name)}</td><td>${r.sent}</td><td>${r.replied}</td>
     <td>${r.pending}</td><td>${r.rate == null ? "—" : r.rate + "%"}</td></tr>`).join("")
    + `</tbody></table>`;
}
$("chan").innerHTML = rateTable(D.by_channel, "Channel");
$("resume").innerHTML = rateTable(D.by_resume, "Resume version");

$("cohort").innerHTML = D.cohorts.length
  ? `<table><thead><tr><th>Week</th><th>Sent</th><th>Replied</th><th>Reply rate</th></tr></thead><tbody>`
    + D.cohorts.map(c => `<tr><td class="name">${esc(c.week)}</td><td>${c.sent}</td>
       <td>${c.replied}</td><td>${c.rate}%</td></tr>`).join("") + `</tbody></table>`
  : `<div class="empty">No data yet.</div>`;

const urgency = d => d >= 21 ? ["critical","overdue"] : d >= 14 ? ["serious","follow up now"]
                   : d >= 10 ? ["warning","can nudge"] : ["good","wait"];
$("follow").innerHTML = D.followups.length
  ? `<table><thead><tr><th>Company</th><th>Stage</th><th>Quiet</th><th>Suggestion</th></tr></thead><tbody>`
    + D.followups.map(f => { const [cls, txt] = urgency(f.quiet);
        return `<tr><td class="name">${esc(f.company)}</td><td>${NAME[f.status] || esc(f.status)}</td>
        <td>${f.quiet} days</td><td><span class="badge ${cls}">${txt}</span></td></tr>`; }).join("")
    + `</tbody></table>`
  : `<div class="empty">Nothing needs a follow-up.</div>`;

$("pipe").innerHTML = D.pipeline.length
  ? `<table><thead><tr><th>Company</th><th>Title</th><th>Stage</th><th>Channel</th>
     <th>Applied</th><th>Days</th></tr></thead><tbody>`
    + D.pipeline.map(p => `<tr><td class="name">${esc(p.company)}</td>
      <td class="name">${p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>` : esc(p.title)}</td>
      <td>${NAME[p.status] || esc(p.status)}</td><td>${esc(p.channel)}</td>
      <td>${esc(p.applied)}</td><td>${p.age}</td></tr>`).join("") + `</tbody></table>`
  : `<div class="empty">No active applications yet.</div>`;
</script>
</body>
</html>
"""


def build(open_after: bool = False) -> str:
    s = stats()
    html = TEMPLATE.replace("__DATA__", json.dumps(s, ensure_ascii=False)) \
                   .replace("__GENERATED__", s["generated_at"])
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    if open_after:
        import subprocess
        subprocess.run(["open", str(DASHBOARD_PATH)], check=False)
    return str(DASHBOARD_PATH)
