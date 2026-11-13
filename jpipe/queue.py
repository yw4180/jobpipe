"""Rolling auto-apply queue.

Runs once a day via launchd (09:30, after the 09:00 fetch). Two phases:

1. process_results() -- consume pending browser-submission result files
   (data/queue/results_*.json) into the applications table. Submitted jobs
   become application records; failed/skipped ones go back to 'new' so they
   resurface. Processed files move to data/queue/done/.

2. generate() -- build today's auto-apply batch:
   - candidates: the golden-rule filter from config/profile.yaml
     (fresh + high score, sponsorship evidence, YoE in range, no senior
     titles, no high-bar companies, strong/good title tiers)
   - each job matched to the best resume version by JD keyword overlap
     (queue.resume_versions in profile.yaml)
   - attach the prefill kit (identity + standard screening answers from
     config/answers.yaml) and a why-this-role draft
   - write data/queue/queue_YYYY-MM-DD.{json,md}; mark jobs 'queued'

The queue JSON is the contract with the submission step (browser task):
URLs, resume file paths, screening answers, why-this-role drafts --
everything needed, no on-the-spot judgment calls.

`python jobpipe.py queue` runs both phases and is safe to run manually any
time (e.g. right after a submission batch, to write the results back).
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from . import score, track
from .config import ANSWERS_PATH, DATA_DIR, companies, profile
from .db import connect, today
from .shortlist import _AVAILABLE, _stories_for

QUEUE_DIR = DATA_DIR / "queue"

_SENIOR_TERMS = ["senior", "sr", "iii", "iv", "staff", "principal"]


def _queue_cfg() -> dict:
    return profile().get("queue", {}) or {}


def _answers() -> dict:
    with open(ANSWERS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _seen_job_ids() -> dict:
    """job_id -> queue date, from every queue file ever written."""
    seen = {}
    if not QUEUE_DIR.exists():
        return seen
    for p in sorted(QUEUE_DIR.glob("queue_*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        day = p.stem.replace("queue_", "", 1)
        for j in data.get("jobs", []):
            if j.get("job_id"):
                seen[j["job_id"]] = day
    return seen


def _reset_stale_queued(conn, seen: dict) -> int:
    """Jobs queued on a previous day that were never submitted (no approval,
    silent failure) go back to 'new' so they can resurface. Today's batch
    is left alone."""
    td = today()
    stale_days = {jid for jid, day in seen.items() if day < td}
    if not stale_days:
        return 0
    rows = conn.execute("SELECT id FROM jobs WHERE status='queued'").fetchall()
    ids = [r["id"] for r in rows if r["id"] in stale_days]
    if not ids:
        return 0
    with conn:
        conn.executemany("UPDATE jobs SET status='new' WHERE id=?", [(i,) for i in ids])
    return len(ids)


def _title_tier(title_l: str) -> str | None:
    titles = profile().get("titles", {}) or {}
    for tier in ("strong", "good", "weak"):
        for pat in titles.get(tier) or []:
            if score.has(title_l, pat):
                return tier
    return None


def match_resume(title: str, description: str | None) -> tuple[str, str]:
    """Pick the resume version whose keyword set overlaps the JD most.
    Returns (version_key, absolute_pdf_path). Ties go to the version listed
    first in config (de). Keywords come from the actual resume text."""
    q = _queue_cfg()
    versions = q.get("resume_versions", {}) or {}
    text = f"{title}\n{description or ''}"
    best_key = q.get("default_version", "de")
    best_n = 0
    for ver, spec in versions.items():
        n = sum(1 for kw in spec.get("keywords", []) if score.has(text, kw))
        if n > best_n:
            best_key, best_n = ver, n
    spec = versions.get(best_key, {})
    return best_key, spec.get("file", "")


def why_role(company: str, hits: list[str]) -> str:
    """Assemble a why-this-role draft: company-domain opener + the top
    project story bodies (first person, from answers.yaml) + closer.
    The submission step may lightly adapt wording; it must not invent facts."""
    ans = _answers()
    openers = ans.get("openers", {}) or {}
    routing = ans.get("company_openers", {}) or {}
    opener = openers.get(routing.get(company, "default"), openers.get("default", ""))
    bodies = ans.get("stories", {}) or {}
    parts = [opener.strip()] if opener else []
    for s in _stories_for(hits)[:2]:
        body = bodies.get(s["name"])
        if body:
            parts.append(body.strip())
    closer = (ans.get("closer", "") or "").strip()
    if closer:
        parts.append(closer)
    return "\n\n".join(parts)


def _golden_candidates(conn, seen: dict, resurfaced: set, limit: int) -> list:
    g = profile().get("golden", {}) or {}
    min_score = g.get("min_score", 60)
    sponsor_ok = sorted(set(g.get("sponsor", ["heavy", "yes", "positive", "low"])))
    fresh_cut = (date.today() - timedelta(days=g.get("second_window_days", 4))).isoformat()
    backlog_cut = (date.today() - timedelta(days=g.get("backlog_max_age", 30))).isoformat()
    backlog_min = g.get("backlog_min_score", 85)
    stretch_yoe = g.get("stretch_yoe", 3)
    stretch_min = g.get("stretch_min_score", 80)
    tier_stretch_min = g.get("tier_stretch_min_score", 75)
    title_tiers_ok = set(g.get("title_tiers", ["strong", "good"]))
    exclude_bar = set(g.get("exclude_bar", ["high"]))
    exclude_senior = g.get("exclude_senior_title", True)
    maxco = (profile().get("thresholds", {}) or {}).get("concurrent_per_company", 3)
    bar_of = {c.get("name"): c.get("bar") for c in companies()}

    sp = {f"sp{i}": v for i, v in enumerate(sponsor_ok)}
    in_clause = ",".join(f":sp{i}" for i in range(len(sponsor_ok)))
    base_params = {"maxco": maxco, **sp}

    fresh = conn.execute(
        f"SELECT * {_AVAILABLE} AND score >= :min_score "
        f"AND sponsor_flag IN ({in_clause}) AND posted_at >= :cut "
        f"ORDER BY score DESC, posted_at DESC",
        {**base_params, "min_score": min_score, "cut": fresh_cut},
    ).fetchall()
    backlog = conn.execute(
        f"SELECT * {_AVAILABLE} AND score >= :min_score "
        f"AND sponsor_flag IN ({in_clause}) AND posted_at >= :cut "
        f"ORDER BY score DESC LIMIT :lim",
        {**base_params, "min_score": backlog_min, "cut": backlog_cut, "lim": limit * 3},
    ).fetchall()

    picked, picked_ids = [], set()

    def keep(r) -> bool:
        if r["id"] in picked_ids:
            return False
        # never queue the same job twice -- unless it was reset to 'new'
        # after going stale (unapproved), in which case it may resurface
        if r["id"] in seen and r["id"] not in resurfaced:
            return False
        title_l = (r["title"] or "").lower()
        if exclude_senior and any(score.has(title_l, t) for t in _SENIOR_TERMS):
            return False
        tier = _title_tier(title_l)
        if tier not in title_tiers_ok:
            if not (tier == "weak" and (r["score"] or 0) >= tier_stretch_min):
                return False
        if bar_of.get(r["company"]) in exclude_bar:
            return False
        detail = json.loads(r["score_detail"] or "{}")
        yoe = detail.get("yoe") or [None, None]
        ymin = yoe[0]
        if ymin is not None:
            if ymin > stretch_yoe:
                return False
            if ymin == stretch_yoe and (r["score"] or 0) < stretch_min:
                return False
        return True

    for r in list(fresh) + list(backlog):
        if len(picked) >= limit:
            break
        if keep(r):
            picked.append(r)
            picked_ids.add(r["id"])
    return picked


def generate(limit: int | None = None) -> list[dict]:
    limit = limit or _queue_cfg().get("max_per_day", 10)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    seen = _seen_job_ids()
    n_reset = _reset_stale_queued(conn, seen)
    # ids that went back to 'new' may legitimately resurface today
    resurfaced = {r["id"] for r in
                  conn.execute("SELECT id FROM jobs WHERE status='new'").fetchall()
                  if r["id"] in seen}
    rows = _golden_candidates(conn, seen, resurfaced, limit)

    ans = _answers()
    kit = {
        "identity": ans.get("identity", {}),
        "standard_answers": [
            {"q": s.get("q"), "a": s.get("a")}
            for s in ans.get("standard", []) or []
        ],
    }
    jobs = []
    for r in rows:
        detail = json.loads(r["score_detail"] or "{}")
        hits = [h for h in detail.get("hits", []) if not h.startswith("-")]
        ver, pdf = match_resume(r["title"], r["description"])
        jobs.append({
            "job_id": r["id"],
            "company": r["company"],
            "title": r["title"],
            "location": r["location"],
            "url": r["url"],
            "ats": r["ats"],
            "score": r["score"],
            "posted_at": r["posted_at"],
            "sponsor_flag": r["sponsor_flag"],
            "resume_version": ver,
            "resume_file": pdf,
            "why_role": why_role(r["company"], hits),
            "keyword_hits": hits[:12],
        })

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": today(),
        "kit": kit,
        "jobs": jobs,
    }
    out = QUEUE_DIR / f"queue_{today()}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [f"# Auto-apply queue · {today()}", "",
          f"{len(jobs)} jobs · approve in chat, then the browser submits them.", ""]
    for i, j in enumerate(jobs, 1):
        md += [
            f"## {i}. [{j['score']}] {j['title']} — {j['company']}",
            f"- {j['location'] or 'location unlisted'} · sponsor: {j['sponsor_flag']} · "
            f"resume: {j['resume_version']} · posted {j['posted_at'] or '?'}",
            f"- {j['url']}", "",
        ]
    (QUEUE_DIR / f"queue_{today()}.md").write_text("\n".join(md), encoding="utf-8")

    if rows:
        with conn:
            conn.executemany("UPDATE jobs SET status='queued' WHERE id=?",
                             [(r["id"],) for r in rows])
    conn.close()
    if n_reset:
        print(f"({n_reset} stale queued jobs reset to 'new')")
    return jobs


def process_results() -> tuple[int, int]:
    """Consume data/queue/results_*.json into the applications table.
    Returns (submitted, back_to_new). Idempotent: processed files move to
    data/queue/done/, and jobs that already have an application are skipped."""
    done_dir = QUEUE_DIR / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    n_sub, n_back = 0, 0
    for path in sorted(QUEUE_DIR.glob("results_*.json")):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"! could not read {path.name}: {e}")
            continue
        conn = connect()
        for it in items:
            jid = it.get("job_id")
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone() if jid else None
            if not job:
                continue
            already = conn.execute(
                "SELECT 1 FROM applications WHERE job_id=?", (jid,)).fetchone()
            if already:
                continue
            if it.get("outcome") == "submitted":
                track.record_application(
                    job, channel="cold",
                    resume=it.get("resume_version") or "de",
                    note=it.get("note") or f"auto-queue {today()}")
                n_sub += 1
            else:
                with conn:
                    conn.execute("UPDATE jobs SET status='new' WHERE id=?", (jid,))
                n_back += 1
        conn.close()
        path.rename(done_dir / path.name)
    return n_sub, n_back


def _wait_for_db(timeout_min: int = 90) -> None:
    """The 09:00/13:00/18:00 fetch can still be running when the 09:30 queue
    job fires (a full fetch takes 1-2h). Wait for the write lock to clear
    instead of crashing into it."""
    deadline = time.time() + timeout_min * 60
    while True:
        try:
            conn = connect()
            conn.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or time.time() >= deadline:
                raise
            print("database is locked (fetch still running?) — retrying in 60s…", flush=True)
            time.sleep(60)


def run(limit: int | None = None) -> None:
    _wait_for_db()
    try:
        n_sub, n_back = process_results()
        if n_sub or n_back:
            print(f"✓ results written back: {n_sub} submitted, {n_back} back to 'new'")
        jobs = generate(limit=limit)
    except sqlite3.OperationalError:
        print("database stayed locked after 90 min of waiting — the fetch is "
              "probably stuck, not just slow. Check data/fetch.log, then rerun.")
        raise
    print(f"\n\033[1mAuto-apply queue ({today()}): {len(jobs)} jobs\033[0m")
    for i, j in enumerate(jobs, 1):
        print(f"{i:>2}. [{j['score']:>3}] {j['title']} — {j['company']} "
              f"(resume: {j['resume_version']}, sponsor: {j['sponsor_flag']})")
    print(f"\n\033[90mQueue file: data/queue/queue_{today()}.json\033[0m")
