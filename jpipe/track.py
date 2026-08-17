"""Application tracking: record entries, update statuses, sweep ghosts, compute metrics.

Metrics come in two classes -- this split is the core design of the whole thing:
  Leading indicators (visible the same day, used to sustain momentum) --
      applications sent, referral asks, follow-ups, streak length
  Lagging indicators (only meaningful after ~two weeks, used to adjust strategy) --
      reply rate by channel / resume version, funnel conversion
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .config import threshold
from .db import (ALL_STATUSES, CHANNELS, FUNNEL, connect, days_ago, log_event,
                 now, today)


# ── Writes ──────────────────────────────────────────────
def record_application(job_row, channel="cold", resume="base", tailored=False, note=None) -> int:
    conn = connect()
    ts = now()
    with conn:
        cur = conn.execute(
            """INSERT INTO applications(job_id, company, title, url, applied_at, channel,
                                        resume_version, tailored, status, status_at, notes)
               VALUES(?,?,?,?,?,?,?,?,'applied',?,?)""",
            (job_row["id"] if job_row else None,
             job_row["company"] if job_row else note,
             job_row["title"] if job_row else "(manual entry)",
             job_row["url"] if job_row else None,
             ts, channel, resume, int(tailored), ts, note),
        )
        app_id = cur.lastrowid
        if job_row:
            conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_row["id"],))
        log_event(conn, "apply", job_row["company"] if job_row else note, app_id,
                  f"{channel} / {resume}")
    conn.close()
    return app_id


def record_manual(company: str, title: str, url=None, channel="cold", resume="base", note=None) -> int:
    """Jobs outside the tracked list (spotted on LinkedIn, came via referral) must be
    recorded too, otherwise the metrics are meaningless."""
    conn = connect()
    ts = now()
    with conn:
        cur = conn.execute(
            """INSERT INTO applications(job_id, company, title, url, applied_at, channel,
                                        resume_version, tailored, status, status_at, notes)
               VALUES(NULL,?,?,?,?,?,?,0,'applied',?,?)""",
            (company, title, url, ts, channel, resume, ts, note),
        )
        app_id = cur.lastrowid
        log_event(conn, "apply", company, app_id, f"{channel} / {resume}")
    conn.close()
    return app_id


def set_status(app_id: int, status: str, note=None) -> bool:
    if status not in ALL_STATUSES:
        raise SystemExit(f"Status must be one of: {', '.join(ALL_STATUSES)}")
    conn = connect()
    row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not row:
        conn.close()
        return False
    with conn:
        conn.execute("UPDATE applications SET status=?, status_at=? WHERE id=?",
                     (status, now(), app_id))
        log_event(conn, "status_change", row["company"], app_id,
                  f"{row['status']} → {status}" + (f" | {note}" if note else ""))
    conn.close()
    return True


def activity(kind: str, company=None, note=None) -> None:
    """Log actions that don't produce an application: referral asks, networking,
    follow-ups. These make up the bulk of the leading indicators."""
    conn = connect()
    with conn:
        log_event(conn, kind, company, None, note)
    conn.close()


def sweep_ghosts() -> int:
    """Still sitting in 'applied' more than ghost_days after submission = silence;
    mark it ghosted. Without this, the reply rate gets diluted by forever-'pending'
    applications until no trend is visible."""
    cutoff = days_ago(threshold("ghost_days", 21))
    conn = connect()
    with conn:
        cur = conn.execute(
            "UPDATE applications SET status='ghosted', status_at=? "
            "WHERE status='applied' AND date(applied_at) < ?",
            (now(), cutoff),
        )
    n = cur.rowcount
    conn.close()
    return n


# ── Reads ───────────────────────────────────────────────
def open_applications() -> list:
    conn = connect()
    rows = conn.execute(
        """SELECT id, company, title, status, channel, resume_version,
                  date(applied_at) AS applied, url,
                  CAST(julianday('now','localtime') - julianday(applied_at) AS INT) AS age
           FROM applications WHERE status NOT IN ('rejected','ghosted','withdrawn')
           ORDER BY CASE status WHEN 'offer' THEN 0 WHEN 'onsite' THEN 1 WHEN 'tech' THEN 2
                                WHEN 'screen' THEN 3 ELSE 4 END, age DESC""",
    ).fetchall()
    conn.close()
    return rows


def _responded(status: str) -> bool:
    """"Responded" = reached any step past applied (rejected counts as a response --
    still better than a ghost)."""
    return status in ("screen", "tech", "onsite", "offer", "rejected")


def export_records() -> list[str]:
    """Export application records as CSV into records/ and commit them to git.

    The data/ directory stays out of version control entirely (JD bodies take too
    much space), but the application history is the one thing that must never be
    lost, so a plain-text copy is exported separately.
    """
    import csv as _csv

    from .config import ROOT

    outdir = ROOT / "records"
    outdir.mkdir(exist_ok=True)
    conn = connect()
    written = []
    for table, cols in [
        ("applications", "id, job_id, company, title, url, applied_at, channel, "
                         "resume_version, tailored, status, status_at, notes"),
        ("events", "id, at, day, kind, company, application_id, note"),
    ]:
        rows = conn.execute(f"SELECT {cols} FROM {table} ORDER BY id").fetchall()
        path = outdir / f"{table}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow([c.strip() for c in cols.split(",")])
            w.writerows([tuple(r) for r in rows])
        written.append(f"{path.name} ({len(rows)} rows)")
    conn.close()
    return written


def stats() -> dict:
    conn = connect()
    apps = conn.execute("SELECT * FROM applications").fetchall()
    events = conn.execute("SELECT * FROM events").fetchall()

    s: dict = {}
    s["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    s["total_apps"] = len(apps)

    # ── Leading indicators: what was done today / this week ──
    td, wk = today(), days_ago(6)
    by_day = defaultdict(lambda: defaultdict(int))
    for e in events:
        by_day[e["day"]][e["kind"]] += 1
    s["today"] = {k: by_day[td].get(k, 0) for k in
                  ("apply", "referral_ask", "networking", "followup", "recruiter_reply")}
    s["week"] = defaultdict(int)
    for day, kinds in by_day.items():
        if day >= wk:
            for k, v in kinds.items():
                s["week"][k] += v
    s["week"] = dict(s["week"])

    # Application streak in days (any day with an apply event counts)
    apply_days = {e["day"] for e in events if e["kind"] == "apply"}
    streak, cursor = 0, date.today()
    while cursor.isoformat() in apply_days:
        streak += 1
        cursor -= timedelta(days=1)
    s["streak"] = streak

    # Applications per day over the last 8 weeks (for the dashboard bar chart)
    s["daily"] = [
        {"day": (date.today() - timedelta(days=i)).isoformat(),
         "n": by_day[(date.today() - timedelta(days=i)).isoformat()].get("apply", 0)}
        for i in range(55, -1, -1)
    ]

    # ── Funnel ──────────────────────────────────────────
    # Reached a stage = current status is at that stage or further along.
    # rejected/ghosted only count as having reached applied.
    reached = {k: 0 for k in FUNNEL}
    for a in apps:
        st = a["status"]
        depth = FUNNEL.index(st) if st in FUNNEL else 0
        for i in range(depth + 1):
            reached[FUNNEL[i]] += 1
    s["funnel"] = [{"stage": k, "n": reached[k]} for k in FUNNEL]

    # ── Lagging indicators: reply rate by channel / resume version ──
    def breakdown(key: str):
        agg = defaultdict(lambda: {"sent": 0, "replied": 0, "pending": 0})
        for a in apps:
            g = agg[a[key] or "—"]
            g["sent"] += 1
            if _responded(a["status"]):
                g["replied"] += 1
            elif a["status"] == "applied":
                g["pending"] += 1
        out = []
        for name, g in sorted(agg.items(), key=lambda x: -x[1]["sent"]):
            decided = g["sent"] - g["pending"]
            out.append({
                "name": name, **g,
                "rate": round(100 * g["replied"] / decided, 1) if decided else None,
            })
        return out

    s["by_channel"] = breakdown("channel")
    s["by_resume"] = breakdown("resume_version")

    # ── Weekly cohorts: of the applications sent that week, how many have replies by now ──
    cohorts = defaultdict(lambda: {"sent": 0, "replied": 0})
    for a in apps:
        wk_key = datetime.fromisoformat(a["applied_at"]).strftime("%G-W%V")
        cohorts[wk_key]["sent"] += 1
        if _responded(a["status"]):
            cohorts[wk_key]["replied"] += 1
    s["cohorts"] = [
        {"week": w, **v, "rate": round(100 * v["replied"] / v["sent"], 1) if v["sent"] else 0}
        for w, v in sorted(cohorts.items())
    ][-10:]

    # ── Needing follow-up ───────────────────────────────
    s["followups"] = [
        dict(r) for r in conn.execute(
            """SELECT id, company, title, status,
                      CAST(julianday('now','localtime') - julianday(status_at) AS INT) AS quiet
               FROM applications
               WHERE status IN ('applied','screen','tech','onsite')
                 AND julianday('now','localtime') - julianday(status_at) >= 7
               ORDER BY quiet DESC LIMIT 12"""
        ).fetchall()
    ]

    # ── Job inventory ───────────────────────────────────
    inv = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN verdict='keep' THEN 1 ELSE 0 END) keep,
                  SUM(CASE WHEN verdict='keep' AND status='new' THEN 1 ELSE 0 END) fresh
           FROM jobs WHERE active=1"""
    ).fetchone()
    s["inventory"] = {"total": inv["total"] or 0, "keep": inv["keep"] or 0, "fresh": inv["fresh"] or 0}

    s["pipeline"] = [dict(r) for r in open_applications()]
    conn.close()
    return s
