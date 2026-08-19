"""Fetch → store → score.

Pull every company's job board concurrently and write incrementally into the
jobs table:
  - new job → insert, first_seen = today
  - existing job → update last_seen and body text
  - not returned this round → active = 0 (the posting was taken down)
Rescore immediately after writing, since profile.yaml may have changed.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import sources
from .config import DB_PATH, companies, profile
from .db import connect, job_id, normalize_company, now, set_meta, today
from .score import score_job
from .sponsor import lookup as sponsor_lookup

WORKERS = 8


def _pull(entry: dict) -> tuple[dict, list[dict], str | None]:
    mod = sources.get(entry)
    if mod is None:
        return entry, [], f"unknown ats: {entry.get('ats')}"
    try:
        return entry, mod.fetch(entry), None
    except Exception as e:
        return entry, [], f"{type(e).__name__}: {e}"


def verify(only: str | None = None) -> None:
    """Probe whether each token is valid. Must be run on first use."""
    targets = [c for c in companies() if not only or only.lower() in c["name"].lower()]
    print(f"Probing {len(targets)} job boards…\n")
    ok, dead = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {}
        for entry in targets:
            mod = sources.get(entry)
            if mod is None:
                dead.append((entry, f"unknown ats: {entry.get('ats')}"))
                continue
            futures[pool.submit(mod.probe, entry)] = entry
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                good, msg = fut.result()
            except Exception as e:
                good, msg = False, f"{type(e).__name__}: {e}"
            (ok if good else dead).append((entry, msg))

    for entry, msg in sorted(ok, key=lambda x: x[0]["name"]):
        print(f"  \033[32m✓\033[0m {entry['name']:<26} {entry['ats']:<11} {msg}")
    if dead:
        print(f"\n\033[31m{len(dead)} broken — remove or fix the token in config/companies.yaml:\033[0m")
        for entry, msg in sorted(dead, key=lambda x: x[0]["name"]):
            print(f"  \033[31m✗\033[0m {entry['name']:<26} token={entry.get('token','?'):<24} {msg}")
    print(f"\nUsable {len(ok)} / {len(targets)}")


def run(only: str | None = None) -> None:
    profile()  # fail fast with the missing-config hint before any network work
    targets = [c for c in companies() if not only or only.lower() in c["name"].lower()]
    conn = connect()
    ts, day = now(), today()
    total_new = total_seen = 0
    failures = []

    # Pass ids of jobs that already have body text to the adapters: the
    # detail-fetch quota goes to jobs that genuinely lack a body (otherwise
    # sources like Meta with a "max 150 details per round" limit would fetch
    # the same batch forever)
    for e in targets:
        # Threshold is 1200 chars rather than non-empty: sources like Apple
        # include a short summary (a few hundred chars) in the listing —
        # that does not count as "has a body"; only a full JD (usually
        # >2000 chars) is skipped
        e["_skip_detail"] = {r[0] for r in conn.execute(
            "SELECT ats_job_id FROM jobs WHERE ats=? AND company=? AND length(description) >= 1200",
            (e["ats"], e["name"]))}

    print(f"Fetching {len(targets)} companies…")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_pull, e) for e in targets]
        for fut in as_completed(futures):
            entry, jobs, err = fut.result()
            if err:
                failures.append((entry["name"], err))
                continue

            company = entry["name"]
            # The first fetch of a company yields its entire backlog, not
            # "just posted" jobs. Mark seeded=1 so historical postings from
            # hundreds of newly added companies don't flood the fresh lane
            # all at once.
            seeding = conn.execute(
                "SELECT 1 FROM jobs WHERE ats=? AND (company=? OR ats IN ('adzuna','hn')) LIMIT 1",
                (entry["ats"], company),
            ).fetchone() is None
            seen_ids = []
            new_here = 0
            with conn:
                for j in jobs:
                    # Aggregator sources (adzuna/hn) carry jobs from many
                    # companies: adapters may override the company name per
                    # job so sponsor records are looked up by the real
                    # employer, not the source
                    co = j.get("company") or company
                    jid = job_id(co, j["ats_job_id"])
                    seen_ids.append(jid)
                    exists = conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
                    if exists:
                        # Body text only upgrades, never downgrades: only a
                        # longer body overwrites (outside the detail quota
                        # this round only has short summaries / empty text,
                        # which must not clobber the full JD in the DB);
                        # likewise dates: null values do not overwrite
                        conn.execute(
                            """UPDATE jobs SET last_seen=?, active=1, title=?, location=?,
                                               url=?,
                                               description=CASE WHEN length(?) > length(COALESCE(description,''))
                                                                THEN ? ELSE description END,
                                               department=?, posted_at=COALESCE(?, posted_at),
                                               ats_updated=? WHERE id=?""",
                            (ts, j["title"], j["location"], j["url"],
                             j["description"], j["description"],
                             j["department"], j["posted_at"], j.get("updated_at"), jid),
                        )
                    else:
                        new_here += 1
                        conn.execute(
                            """INSERT INTO jobs(id, company, ats, ats_job_id, title, location, url,
                                                department, posted_at, ats_updated, description,
                                                first_seen, last_seen, active, seeded, status)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'new')""",
                            (jid, co, entry["ats"], j["ats_job_id"], j["title"], j["location"],
                             j["url"], j["department"], j["posted_at"], j.get("updated_at"),
                             j["description"], ts, ts, 1 if seeding else 0),
                        )
                # Not returned this round = taken down (except windowed
                # sources: they only see the most recent slice)
                windowed = getattr(sources.get(entry), "WINDOWED", False)
                if seen_ids and not windowed:
                    ph = ",".join("?" * len(seen_ids))
                    # Scoped by ats: same-named company jobs found via
                    # aggregator sources (adzuna/hn) are not affected by the
                    # company's own board takedown cleanup (and vice versa)
                    conn.execute(
                        f"UPDATE jobs SET active=0 WHERE company=? AND ats=? AND id NOT IN ({ph})",
                        [company, entry["ats"], *seen_ids],
                    )
            total_new += new_here
            total_seen += len(jobs)
            flag = f"  \033[36m+{new_here} new\033[0m" if new_here else ""
            print(f"  {company:<26} {len(jobs):>4} jobs{flag}")

    if failures:
        print(f"\n\033[33m{len(failures)} companies failed to fetch (run verify to check for dead tokens):\033[0m")
        for name, err in failures:
            print(f"  ! {name}: {err}")

    with conn:
        set_meta(conn, "last_fetch", ts)
    print(f"\n{total_seen} active jobs total, {total_new} of them new. Scoring…")
    rescore(conn)
    conn.close()

    # Second pass: LLM screening (only processes new/changed JDs; silently
    # skipped when no credentials are configured)
    from . import enrich
    enrich.run(auto=True)


def prune() -> None:
    """Delete jobs that are inactive and past stale_days, then VACUUM.

    JD bodies take a lot of space (10k+ jobs is roughly 80MB). Jobs that
    were applied to are kept forever — the applications table relies on
    them to show what was applied to.
    """
    from .config import threshold
    from .db import days_ago

    conn = connect()
    before = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    with conn:
        cur = conn.execute(
            """DELETE FROM jobs
               WHERE active=0 AND date(last_seen) < ?
                 AND id NOT IN (SELECT job_id FROM applications WHERE job_id IS NOT NULL)""",
            (days_ago(threshold("stale_days", 45)),),
        )
        n = cur.rowcount
    conn.execute("VACUUM")
    conn.close()
    after = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print(f"✓ Deleted {n} stale jobs, DB size {before/1e6:.0f}MB → {after/1e6:.0f}MB")


def rescore(conn=None) -> None:
    """Rescore every active job against the current profile.yaml. Run this alone after config changes."""
    own = conn is None
    conn = conn or connect()

    # YAML parses bare yes/no as booleans; normalize back to strings
    def _flag(v):
        if v is True:
            return "yes"
        if v is False:
            return "none"
        return str(v or "unknown")

    seeds = {
        normalize_company(c["name"]): (_flag(c.get("sponsor")), c.get("aliases") or [],
                                       c.get("bar"))
        for c in companies()
    }
    rows = conn.execute("SELECT * FROM jobs WHERE active=1").fetchall()

    kept = rejected = 0
    reasons: dict[str, int] = {}
    with conn:
        for r in rows:
            seed, aliases, bar = seeds.get(normalize_company(r["company"]),
                                           ("unknown", [], None))
            flag = sponsor_lookup(conn, r["company"], fallback=seed, aliases=aliases)
            verdict, sc, detail = score_job(dict(r), company_sponsor=flag, company_bar=bar)
            if verdict == "reject":
                rejected += 1
                reasons[detail.get("reason", "?")] = reasons.get(detail.get("reason", "?"), 0) + 1
            else:
                kept += 1
            conn.execute(
                """UPDATE jobs SET score=?, score_detail=?, verdict=?, reject_reason=?,
                                   sponsor_flag=? WHERE id=?""",
                (sc, json.dumps(detail, ensure_ascii=False), verdict,
                 detail.get("reason"), detail.get("sponsor_flag", flag), r["id"]),
            )

    # Jobs posted more than archive_days ago are dropped across the board:
    # they are almost certainly no longer accepting new resumes.
    # Runs after the scoring loop (which rewrites verdict) and re-executes on
    # every rescore, keeping the DB rolling.
    from .config import threshold
    # High-scoring roles are exempt: a company that reposts a strong job
    # keeps an old posted_at (Amazon repost example), so archiving by date
    # would drop a still-active good job. Let those land in the Backlog
    # section instead; only low-score aged jobs are cleared.
    keep_score = (profile().get("golden", {}) or {}).get("backlog_min_score", 85)
    with conn:
        arch = conn.execute(
            """UPDATE jobs SET verdict='reject', reject_reason='Posted over 30 days ago (archive)'
               WHERE active=1 AND verdict='keep' AND score < ?
                 AND date(COALESCE(posted_at, first_seen)) < date('now','localtime',?)""",
            (keep_score, f"-{threshold('archive_days', 30)} day",),
        ).rowcount

    # LLM-screening vetoes must be reapplied too (the loop above rewrites
    # verdict back to keep)
    from . import enrich
    vetoed = enrich.apply_vetoes(conn)

    print(f"Scoring done: kept {kept - arch - vetoed}, rejected {rejected}, "
          f"archived {arch} (posted >30 days ago)"
          + (f", LLM vetoed {vetoed}" if vetoed else ""))
    if rejected:
        print("  Top 5 reject reasons:")
        for reason, n in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
            print(f"    {n:>5}  {reason}")
    if own:
        conn.close()
