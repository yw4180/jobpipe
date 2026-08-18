#!/usr/bin/env python3
"""Job board, web edition.

Reuses the repo's jpipe package directly: it reads the same pipeline.db,
and status transitions go through track.set_status (which writes events as
usual), so no business logic is duplicated.
Golden-job admission criteria come from the `golden` section of profile.yaml.

    python3 app.py          # http://127.0.0.1:5175
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402
from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from jpipe import track  # noqa: E402
from jpipe.config import ANSWERS_PATH, PROFILE_PATH, profile  # noqa: E402
from jpipe.db import ALL_STATUSES, CHANNELS, FUNNEL, TERMINAL, connect  # noqa: E402

app = Flask(__name__, static_folder="static")

# The board only allows these three transitions on the jobs table;
# applied is managed by jobpipe apply.
JOB_STATUSES = {"new", "shortlisted", "skipped"}

# A job's "effective date": true posting date > recruiter's last edit > first seen by us
EFFECTIVE_DATE = "COALESCE(posted_at, ats_updated, first_seen)"


# A job's "freshness date" (used by the golden window): true posting date;
# if absent, the job must not be a seeded (pre-existing) one.
FRESH_DATE = ("COALESCE(posted_at, CASE WHEN seeded=0 THEN first_seen END,"
              " '1970-01-01')")

# LinkedIn-style classifier: role type (by title, first match wins, order matters)
ROLE_CASE = """CASE
  WHEN lower(title) LIKE '%analytics engineer%' THEN 'ae'
  WHEN lower(title) LIKE '%data engineer%' OR lower(title) LIKE '%etl%'
    OR lower(title) LIKE '%data pipeline%' OR lower(title) LIKE '%big data%'
    OR lower(title) LIKE '%data platform%' OR lower(title) LIKE '%data infrastructure%'
    THEN 'de'
  WHEN lower(title) LIKE '%machine learning%' OR lower(title) LIKE '%ml engineer%'
    OR lower(title) LIKE '%mlops%' OR lower(title) LIKE '%ml platform%'
    OR lower(title) LIKE '%ml infra%' THEN 'mle'
  WHEN lower(title) LIKE '%data scientist%' THEN 'ds'
  WHEN lower(title) LIKE '%business intelligence%' OR lower(title) LIKE '%bi engineer%'
    THEN 'bi'
  ELSE 'swe' END"""

# Seniority: senior uses the word-boundary flag set at scoring time (reliable),
# junior uses title signals, everything else counts as mid.
SENIORITY_CASE = """CASE
  WHEN json_extract(llm,'$.seniority') IN ('junior','mid','senior') THEN json_extract(llm,'$.seniority')
  WHEN COALESCE(json_extract(score_detail,'$.senior_title'),0)=1 THEN 'senior'
  WHEN lower(title) LIKE '%junior%' OR lower(title) LIKE '%entry%'
    OR lower(title) LIKE '%new grad%' OR lower(title) LIKE '%university grad%'
    OR lower(title) LIKE '%associate%' OR lower(title) LIKE '% i' THEN 'junior'
  ELSE 'mid' END"""


def _golden_where(window: tuple[int, int] | None = None) -> tuple[str, list]:
    """Turn the `golden` section of profile.yaml into SQL conditions.
    Config changes need no code changes here.

    window=(start, end) in days: TOP section (0, max_age_days),
    catch-up section (max_age_days, second_window_days).
    """
    g = profile().get("golden", {})
    lo, hi = window or (0, g.get("max_age_days", 2))
    sponsors = g.get("sponsor", ["heavy", "yes", "positive"])
    yoe = "COALESCE(json_extract(llm,'$.min_yoe'), json_extract(score_detail,'$.yoe[0]'))"
    if lo == 0:
        # TOP: <= max_req_yoe, or stretch_yoe years with an exceptional score
        # (high scorers should not be blocked by the filter).
        yoe_cond = (f"({yoe} IS NULL OR {yoe} <= ? OR ({yoe} <= ? AND score >= ?))")
        yoe_params = [g.get("max_req_yoe", 2), g.get("stretch_yoe", 3),
                      g.get("stretch_min_score", 80)]
    else:
        # Catch-up: uniformly relaxed to stretch_yoe
        yoe_cond = f"({yoe} IS NULL OR {yoe} <= ?)"
        yoe_params = [g.get("stretch_yoe", 3)]
    conds = [
        f"date({FRESH_DATE}) >= date('now', 'localtime', ?)",
        "score >= ?",
        f"sponsor_flag IN ({','.join('?' * len(sponsors))})",
        yoe_cond,
    ]
    params: list = [f"-{hi} day", g.get("min_score", 60), *sponsors, *yoe_params]
    if lo:
        conds.append(f"date({FRESH_DATE}) < date('now', 'localtime', ?)")
        params.append(f"-{lo} day")
    if g.get("exclude_senior_title"):
        conds.append("COALESCE(json_extract(score_detail,'$.senior_title'),0)=0")
        conds.append("COALESCE(json_extract(llm,'$.seniority'),'') != 'senior'")
    exclude_bar = g.get("exclude_bar") or []
    if exclude_bar:
        conds.append(
            "COALESCE(json_extract(score_detail,'$.bar'),'mid')"
            f" NOT IN ({','.join('?' * len(exclude_bar))})")
        params += exclude_bar
    tiers = g.get("title_tiers") or []
    if tiers:
        conds.append(
            f"json_extract(score_detail,'$.title_tier') IN ({','.join('?' * len(tiers))})")
        params += tiers
    return " AND ".join(conds), params


def _answers() -> dict:
    """Answer-sheet data source, re-read every time (the file is small;
    edits take effect on the next page refresh)."""
    try:
        with open(ANSWERS_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _matched_stories(det: dict) -> list[dict]:
    """Rank project stories by score_detail keyword hits
    (same logic as the expanded card detail)."""
    hit_set = {h.lstrip("+-") for h in det.get("hits", [])}
    out = []
    for s in profile().get("stories", []):
        n = sum(1 for t in s.get("triggers", [])
                if any(t.rstrip("*") == h or t.rstrip("*") in h or h in t
                       for h in hit_set))
        if n:
            out.append({"name": s["name"], "pitch": s["pitch"], "matches": n})
    out.sort(key=lambda x: -x["matches"])
    return out


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/jobs")
def api_jobs():
    q = request.args.get("q", "").strip()
    sponsor = [s for s in request.args.get("sponsor", "").split(",") if s]
    status = request.args.get("status", "").strip()
    min_score = request.args.get("min_score", type=int)
    max_age = request.args.get("max_age", type=int)      # in days; omitted = no limit
    max_yoe = request.args.get("max_yoe", type=int)      # JD minimum YoE <= N
    roles = [r for r in request.args.get("role", "").split(",") if r]
    seniority = [s for s in request.args.get("seniority", "").split(",") if s]
    golden = request.args.get("golden", "")              # "1"=TOP  "2"=catch-up
    limit = min(request.args.get("limit", 50, type=int), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)

    where = ["active=1", "verdict='keep'"]
    params: list = []
    if golden:
        g = profile().get("golden", {})
        top_d = g.get("max_age_days", 2)
        window = (0, top_d) if golden == "1" else (top_d, g.get("second_window_days", 4))
        gw, gp = _golden_where(window)
        where.append(f"({gw})")
        params += gp
    if roles:
        where.append(f"({ROLE_CASE}) IN ({','.join('?' * len(roles))})")
        params += roles
    if seniority:
        where.append(f"({SENIORITY_CASE}) IN ({','.join('?' * len(seniority))})")
        params += seniority
    if max_yoe is not None:
        where.append("(COALESCE(json_extract(llm,'$.min_yoe'), json_extract(score_detail,'$.yoe[0]')) IS NULL"
                     " OR COALESCE(json_extract(llm,'$.min_yoe'), json_extract(score_detail,'$.yoe[0]')) <= ?)")
        params.append(max_yoe)
    if q:
        where.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
        params += [f"%{q}%"] * 3
    if sponsor:
        where.append(f"sponsor_flag IN ({','.join('?' * len(sponsor))})")
        params += sponsor
    if status:
        where.append("status=?")
        params.append(status)
    if min_score is not None:
        where.append("score>=?")
        params.append(min_score)
    if max_age is not None:
        where.append(f"date({EFFECTIVE_DATE}) >= date('now', 'localtime', ?)")
        params.append(f"-{max_age} day")

    conn = connect()
    sql_where = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {sql_where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT id, company, title, location, url, score, sponsor_flag, status,
                   date({EFFECTIVE_DATE}) AS posted,
                   CAST(julianday('now', 'localtime') - julianday({EFFECTIVE_DATE}) AS INTEGER) AS age,
                   COALESCE(json_extract(score_detail,'$.bar'),'mid') AS bar,
                   COALESCE(json_extract(llm,'$.min_yoe'), json_extract(score_detail,'$.yoe[0]')) AS req_yoe,
                   {ROLE_CASE} AS role,
                   {SENIORITY_CASE} AS seniority
            FROM jobs WHERE {sql_where}
            ORDER BY score DESC, posted DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    return jsonify(total=total, offset=offset,
                   has_more=offset + len(rows) < total,
                   jobs=[dict(r) for r in rows])


@app.get("/api/jobs/<job_id>")
def api_job_detail(job_id):
    """Expanded card: score breakdown + keyword hits + story routing + JD preview."""
    conn = connect()
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not r:
        return jsonify(error="Job not found"), 404
    det = json.loads(r["score_detail"] or "{}")
    hits = [h.lstrip("+-") for h in det.get("hits", [])]
    stories = _matched_stories(det)

    desc = (r["description"] or "").strip()
    try:
        llm = json.loads(r["llm"]) if r["llm"] else None
    except (ValueError, KeyError, IndexError):
        llm = None
    return jsonify(
        llm=llm,
        parts=det.get("parts", {}),
        hits=hits,
        yoe=det.get("yoe"),
        bar=det.get("bar", "mid"),
        title_tier=det.get("title_tier"),
        location_tier=det.get("location_tier"),
        snippet=det.get("snippet"),        # verbatim sponsorship snippet from the JD
        stories=stories[:2],
        desc_preview=desc[:2000],
        desc_truncated=len(desc) > 2000,
    )


@app.post("/api/jobs/<job_id>/status")
def api_job_status(job_id):
    status = (request.json or {}).get("status")
    if status not in JOB_STATUSES:
        return jsonify(error=f"status must be one of {sorted(JOB_STATUSES)}"), 400
    conn = connect()
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status=? WHERE id=? AND status IN ('new','shortlisted','skipped')",
            (status, job_id),
        )
    conn.close()
    if cur.rowcount == 0:
        return jsonify(error="Job not found, or already applied (managed by jobpipe apply)"), 404
    return jsonify(ok=True, status=status)


@app.get("/api/applications")
def api_applications():
    conn = connect()
    rows = conn.execute(
        """SELECT id, company, title, url, channel, resume_version, tailored, status, notes,
                  date(applied_at) AS applied,
                  CAST(julianday('now', 'localtime') - julianday(applied_at) AS INTEGER) AS age
           FROM applications ORDER BY status_at DESC"""
    ).fetchall()
    conn.close()
    return jsonify(
        applications=[dict(r) for r in rows],
        funnel=FUNNEL, terminal=TERMINAL,
    )


@app.post("/api/applications/<int:app_id>/status")
def api_app_status(app_id):
    body = request.json or {}
    status, note = body.get("status"), body.get("note") or None
    if status not in ALL_STATUSES:
        return jsonify(error=f"status must be one of {ALL_STATUSES}"), 400
    if not track.set_status(app_id, status, note):
        return jsonify(error=f"No application #{app_id}"), 404
    return jsonify(ok=True, status=status)


@app.get("/api/queue")
def api_queue():
    """Apply-mode todo queue, three tiers: saved -> TOP (48h) -> catch-up (48-96h)."""
    g = profile().get("golden", {})
    top_d = g.get("max_age_days", 2)
    w1, p1 = _golden_where((0, top_d))
    w2, p2 = _golden_where((top_d, g.get("second_window_days", 4)))
    conn = connect()
    base = f"""SELECT id, company, title, location, url, score, sponsor_flag, status,
                      date({EFFECTIVE_DATE}) AS posted, '{{tier}}' AS tier
               FROM jobs WHERE active=1 AND verdict='keep' AND {{cond}}
               ORDER BY score DESC"""
    seen, queue = set(), []
    for tier, cond, params in [
        ("pinned", "status='shortlisted'", []),
        ("top",    f"status='new' AND ({w1})", p1),
        ("catchup", f"status='new' AND ({w2})", p2),
    ]:
        for r in conn.execute(base.format(tier=tier, cond=cond), params):
            if r["id"] not in seen:
                seen.add(r["id"])
                queue.append(dict(r))
    conn.close()
    return jsonify(queue=queue, channels=CHANNELS)


@app.get("/api/answers")
def api_answers():
    return jsonify(standard=_answers().get("standard", []))


@app.get("/api/jobs/<job_id>/draft")
def api_job_draft(job_id):
    """Assemble a why-this-role draft from the JD:
    opener (company/domain) -> story paragraphs -> closer."""
    conn = connect()
    r = conn.execute("SELECT company, score_detail FROM jobs WHERE id=?",
                     (job_id,)).fetchone()
    conn.close()
    if not r:
        return jsonify(error="Job not found"), 404
    a = _answers()
    det = json.loads(r["score_detail"] or "{}")
    hits = {h.lstrip("+-") for h in det.get("hits", [])}

    openers = a.get("openers", {})
    key = (a.get("company_openers") or {}).get(r["company"])
    if not key:
        if hits & {"healthcare", "biotech"}:
            key = "healthcare"
        elif hits & {"ads", "advertising", "ctr", "recommendation", "ranking"}:
            key = "ads"
        else:
            key = "default"
    paragraphs = [openers.get(key) or openers.get("default", "")]

    story_texts = a.get("stories", {})
    used = []
    for s in _matched_stories(det)[:2]:
        if s["name"] in story_texts:
            paragraphs.append(story_texts[s["name"]])
            used.append(s["name"])
    if not used and story_texts:
        first = next(iter(story_texts))
        paragraphs.append(story_texts[first])
        used.append(first)
    paragraphs.append(a.get("closer", ""))

    return jsonify(draft="\n\n".join(p for p in paragraphs if p),
                   opener=key, stories=used)


@app.post("/api/jobs/<job_id>/apply")
def api_job_apply(job_id):
    """Log an application: goes through track.record_application (writes
    applications + events and sets jobs.status to applied), fully equivalent
    to `jobpipe.py apply`."""
    body = request.json or {}
    channel = body.get("channel", "cold")
    if channel not in CHANNELS:
        return jsonify(error=f"channel must be one of {CHANNELS}"), 400
    conn = connect()
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not r:
        return jsonify(error="Job not found"), 404
    if r["status"] == "applied":
        return jsonify(error="This job has already been logged"), 409
    app_id = track.record_application(
        dict(r), channel, body.get("resume", "base"),
        bool(body.get("tailored")), body.get("note") or None)
    return jsonify(ok=True, application_id=app_id)


@app.get("/api/overview")
def api_overview():
    """Overview page data: activity heatmap (16 weeks) + daily new jobs (21 days) + stat tiles."""
    conn = connect()
    heat = [dict(r) for r in conn.execute(
        """SELECT day, SUM(kind='apply') AS applies, COUNT(*) AS total
           FROM events WHERE day >= date('now', 'localtime', '-111 day')
           GROUP BY day""")]
    newjobs = [dict(r) for r in conn.execute(
        """SELECT date(first_seen) AS d,
                  SUM(CASE WHEN score >= 60 THEN 1 ELSE 0 END) AS hi,
                  COUNT(*) AS n
           FROM jobs WHERE seeded=0 AND verdict='keep' AND active=1
             AND date(first_seen) >= date('now', 'localtime', '-20 day')
           GROUP BY d ORDER BY d""")]
    t = {}
    t["keep"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE active=1 AND verdict='keep'").fetchone()[0]
    t["new_today"] = conn.execute(
        """SELECT COUNT(*) FROM jobs WHERE active=1 AND verdict='keep' AND seeded=0
           AND date(first_seen) = date('now', 'localtime')""").fetchone()[0]
    t["new_week"] = conn.execute(
        """SELECT COUNT(*) FROM jobs WHERE active=1 AND verdict='keep' AND seeded=0
           AND date(first_seen) >= date('now', 'localtime', '-6 day')""").fetchone()[0]
    t["apps_week"] = conn.execute(
        """SELECT COUNT(*) FROM events WHERE kind='apply'
           AND day >= date('now', 'localtime', '-6 day')""").fetchone()[0]
    t["acts_week"] = conn.execute(
        """SELECT COUNT(*) FROM events WHERE kind != 'apply'
           AND day >= date('now', 'localtime', '-6 day')""").fetchone()[0]
    t["apps_total"] = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    t["open_apps"] = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status IN "
        "('applied','screen','tech','onsite')").fetchone()[0]
    t["llm_cov"] = conn.execute(
        """SELECT ROUND(100.0 * SUM(llm IS NOT NULL) / COUNT(*))
           FROM jobs WHERE active=1 AND verdict='keep'""").fetchone()[0] or 0
    conn.close()
    return jsonify(heat=heat, newjobs=newjobs, tiles=t)


@app.get("/api/stats")
def api_stats():
    conn = connect()
    g = profile().get("golden", {})
    top_d = g.get("max_age_days", 2)
    w1, p1 = _golden_where((0, top_d))
    w2, p2 = _golden_where((top_d, g.get("second_window_days", 4)))
    golden_new = conn.execute(
        f"""SELECT COUNT(*) FROM jobs
            WHERE active=1 AND verdict='keep' AND status='new' AND {w1}""", p1
    ).fetchone()[0]
    catchup_new = conn.execute(
        f"""SELECT COUNT(*) FROM jobs
            WHERE active=1 AND verdict='keep' AND status='new' AND {w2}""", p2
    ).fetchone()[0]
    jobs = dict(conn.execute(
        "SELECT status, COUNT(*) FROM jobs WHERE active=1 AND verdict='keep' GROUP BY status"
    ).fetchall())
    funnel = dict(conn.execute(
        "SELECT status, COUNT(*) FROM applications GROUP BY status"
    ).fetchall())
    week = conn.execute(
        """SELECT kind, COUNT(*) FROM events
           WHERE day >= date('now', 'localtime', '-6 day') GROUP BY kind"""
    ).fetchall()
    conn.close()
    return jsonify(golden_new=golden_new, catchup_new=catchup_new,
                   jobs=jobs, funnel=funnel,
                   week={k: n for k, n in week}, funnel_order=FUNNEL)




# ── Manual fetch with live progress ─────────────────────────────
import re as _re
import subprocess as _sp
import threading as _th
import time as _time
from collections import deque as _deque

_SCORE_EST, _ENRICH_EST = 130, 90   # seconds, rough phase estimates for ETA

FETCH_STATE = {
    "running": False, "phase": "idle", "done": 0, "total": 0,
    "started": None, "phase_started": None, "ended": None, "rc": None,
    "log": _deque(maxlen=12),
}
_FETCH_LOCK = _th.Lock()


def _fetch_worker():
    st = FETCH_STATE
    proc = _sp.Popen([sys.executable, "-u", str(REPO_ROOT / "jobpipe.py"), "fetch"],
                     stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                     cwd=REPO_ROOT, bufsize=1)
    ansi = _re.compile(r"\x1b\[[0-9;]*m")
    for raw in proc.stdout:
        line = ansi.sub("", raw.rstrip())
        if not line.strip():
            continue
        st["log"].append(line)
        m = _re.search(r"Fetching (\d+) companies", line)
        if m:
            st["total"] = int(m.group(1))
        elif _re.match(r"\s{2}\S.*\s\d+\s+jobs", line):
            st["done"] += 1
        elif "Scoring" in line:
            st["phase"], st["phase_started"] = "scoring", _time.time()
        elif "LLM refinement" in line:
            st["phase"], st["phase_started"] = "enrich", _time.time()
    proc.wait()
    st.update(rc=proc.returncode, ended=_time.time(), running=False,
              phase="done" if proc.returncode == 0 else "error")


@app.post("/api/fetch/start")
def api_fetch_start():
    with _FETCH_LOCK:
        if FETCH_STATE["running"]:
            return jsonify(error="a fetch is already running"), 409
        FETCH_STATE.update(running=True, phase="fetching", done=0, total=0,
                           rc=None, ended=None, started=_time.time(),
                           phase_started=_time.time())
        FETCH_STATE["log"].clear()
        _th.Thread(target=_fetch_worker, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/fetch/status")
def api_fetch_status():
    st = FETCH_STATE
    now = _time.time()
    eta = None
    if st["running"] and st["started"]:
        if st["phase"] == "fetching" and st["done"] >= 3:
            rate = st["done"] / max(now - st["started"], 1)
            eta = (st["total"] - st["done"]) / rate + _SCORE_EST + _ENRICH_EST
        elif st["phase"] == "scoring":
            eta = max(_SCORE_EST - (now - st["phase_started"]), 10) + _ENRICH_EST
        elif st["phase"] == "enrich":
            eta = max(_ENRICH_EST - (now - st["phase_started"]), 10)
    return jsonify(
        running=st["running"], phase=st["phase"], done=st["done"],
        total=st["total"], rc=st["rc"],
        elapsed=round(now - st["started"]) if st["started"] else None,
        eta=round(eta) if eta else None,
        log=list(st["log"]),
    )


if __name__ == "__main__":
    if not PROFILE_PATH.exists():
        raise SystemExit(
            "No config found. Run `python jobpipe.py init` in the repo root first."
        )
    app.run(host="127.0.0.1", port=int(os.environ.get("BOARD_PORT", "5175")),
            debug=False)
