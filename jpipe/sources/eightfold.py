"""Eightfold job board public API (the platform behind custom career sites such as Netflix's).

    GET https://{host}/api/apply/v2/jobs?domain={domain}&query=...&num=...&start=...
    GET https://{host}/api/apply/v2/jobs/{id}?domain={domain}     <- the JD body lives in the detail endpoint

The `site` field in config has the format "{host}/{domain}",
e.g. explore.jobs.netflix.net/netflix.com

The listing endpoint carries no JD, so the body has to be fetched per job —
hence the MAX_DETAIL cap plus rate limiting, to avoid hammering their endpoint.
"""

from __future__ import annotations

import time
from datetime import date

TIMEOUT = 30
PAGE = 50
MAX_PAGES = 4
MAX_DETAIL = 250     # max number of job details fetched per fetch run
SLEEP = 0.15
SEARCH_TERMS = ["data engineer", "machine learning engineer", "data platform"]


def _parts(entry: dict) -> tuple[str, str]:
    site = entry.get("site") or ""
    host, _, domain = site.partition("/")
    if not host or not domain:
        raise ValueError(f"eightfold site must have the form 'host/domain', got {site!r}")
    return f"https://{host}/api/apply/v2/jobs", domain


def _epoch_date(v) -> str | None:
    try:
        return date.fromtimestamp(int(v)).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        base, domain = _parts(entry)
    except ValueError as e:
        return False, str(e)
    try:
        r = session().get(base, params={"domain": domain, "query": "data engineer",
                                        "num": 1, "start": 0}, timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        n = r.json().get("count")
    except Exception:
        return False, "response is not JSON"
    return True, f"data engineer: {n} hits"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    s = session()
    base, domain = _parts(entry)
    seen: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            r = s.get(base, params={"domain": domain, "query": term,
                                    "num": PAGE, "start": page * PAGE},
                      timeout=TIMEOUT)
            r.raise_for_status()
            positions = r.json().get("positions") or []
            for p in positions:
                pid = str(p.get("id"))
                if pid in seen:
                    continue
                seen[pid] = {
                    "ats_job_id": pid,
                    "title": (p.get("name") or "").strip(),
                    "location": "; ".join(p.get("locations") or [])
                                or (p.get("location") or ""),
                    "url": p.get("canonicalPositionUrl"),
                    "department": p.get("department"),
                    "posted_at": _epoch_date(p.get("t_create")),
                    "updated_at": _epoch_date(p.get("t_update")),
                    "description": "",
                }
            if len(positions) < PAGE:
                break

    # The JD body lives in the detail endpoint; backfill one by one with rate
    # limiting (jobs that already have a body are skipped so the quota goes to
    # those missing one)
    skip = entry.get("_skip_detail") or set()
    todo = [(pid, j) for pid, j in seen.items() if pid not in skip]
    for i, (pid, job) in enumerate(todo):
        if i >= MAX_DETAIL:
            break
        try:
            r = s.get(f"{base}/{pid}", params={"domain": domain}, timeout=TIMEOUT)
            if r.ok:
                job["description"] = clean_html(r.json().get("job_description"))
        except Exception:
            pass  # a missing body only reduces scoring precision — not worth failing the whole fetch run
        time.sleep(SLEEP)
    return list(seen.values())
