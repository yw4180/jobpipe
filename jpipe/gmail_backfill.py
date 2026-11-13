"""One-time backfill: Gmail application confirmations -> applications table.

Input: a JSON list produced by the Gmail search step (company/title/url +
the confirmation email's date):
    [{"company": "Adobe", "title": "Software Engineer New Grad",
      "url": "https://...", "applied_at": "2026-09-10",
      "channel": "cold", "note": "confirmation email"}, ...]

For each entry: find the best matching job row (normalized company + title
similarity). If found and no application exists for it yet, record the
application against the job (jobs.status -> 'applied'); otherwise insert a
standalone application (job_id NULL) so the ledger still dedups it.
Idempotent: reruns skip entries that already have a matching application.

    python jobpipe.py gmail-backfill --input data/gmail_backfill.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import DATA_DIR
from .db import connect, log_event, normalize_company, now


def _title_score(a: str, b: str) -> int:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0
    if a == b:
        return 3
    if a in b or b in a:
        return 2
    ta, tb = set(a.split()), set(b.split())
    return 1 if len(ta & tb) >= 3 else 0


def _best_job(conn, company: str, title: str):
    cnorm = normalize_company(company)
    best, best_s = None, 0
    for r in conn.execute("SELECT * FROM jobs"):
        if normalize_company(r["company"]) != cnorm:
            continue
        s = _title_score(r["title"], title)
        if s > best_s:
            best, best_s = r, s
    return best if best_s >= 2 else None


def _already(conn, company: str, title: str) -> bool:
    cnorm, tlow = normalize_company(company), (title or "").lower().strip()
    for r in conn.execute("SELECT company, title FROM applications"):
        if normalize_company(r["company"]) == cnorm \
                and (r["title"] or "").lower().strip() == tlow:
            return True
    return False


def run(path: str | None = None) -> None:
    p = Path(path or DATA_DIR / "gmail_backfill.json")
    if not p.exists():
        sys.exit(f"input not found: {p}")
    items = json.loads(p.read_text(encoding="utf-8"))
    conn = connect()
    n_match = n_manual = n_skip = 0
    for it in items:
        company = (it.get("company") or "").strip()
        title = (it.get("title") or "").strip()
        if not company or not title or _already(conn, company, title):
            n_skip += 1
            continue
        job = _best_job(conn, company, title)
        jid = job["id"] if job else None
        note = (it.get("note") or "gmail backfill").strip()
        status = (it.get("status") or "applied").strip() or "applied"
        with conn:
            cur = conn.execute(
                """INSERT INTO applications(job_id, company, title, url, applied_at,
                                            channel, resume_version, tailored,
                                            status, status_at, notes)
                   VALUES(?,?,?,?,?,?, 'unknown', 0, ?, ?, ?)""",
                (jid, company, title, it.get("url"),
                 it.get("applied_at") or now()[:10],
                 it.get("channel") or "cold", status, now(), note),
            )
            if jid:
                conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (jid,))
            log_event(conn, "apply", company, cur.lastrowid, note + " [gmail]")
        if jid:
            n_match += 1
        else:
            n_manual += 1
    conn.close()
    print(f"\u2713 gmail backfill: {n_match} matched to tracked jobs, "
          f"{n_manual} standalone, {n_skip} skipped (already recorded)")
