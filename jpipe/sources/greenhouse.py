"""Greenhouse public job board API.

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
"""

from __future__ import annotations

BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
TIMEOUT = 25


def _url(entry: dict) -> str:
    return BASE.format(token=entry["token"])


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(_url(entry), timeout=TIMEOUT)
    except Exception as e:  # network problems do not mean the token is invalid
        return False, f"network: {type(e).__name__}"
    if r.status_code == 404:
        return False, "404 token does not exist"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        n = len(r.json().get("jobs", []))
    except Exception:
        return False, "response is not JSON"
    return True, f"{n} jobs"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    r = session().get(_url(entry), timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        depts = j.get("departments") or []
        out.append({
            "ats_job_id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or "").strip(),
            "url": j.get("absolute_url"),
            "department": ", ".join(d.get("name", "") for d in depts) or None,
            # first_published must take precedence. updated_at means "last
            # modified" — a single-character edit by the recruiter refreshes it,
            # which would make a job posted 4 months ago look like last week's
            # and outright distort freshness sorting.
            "posted_at": (j.get("first_published") or j.get("updated_at") or "")[:10] or None,
            # updated_at is kept separately: a recent re-edit by the recruiter
            # usually means the req is still alive, so it serves as a secondary
            # freshness signal (weighted below posted_at).
            "updated_at": (j.get("updated_at") or "")[:10] or None,
            "description": clean_html(j.get("content")),
        })
    return out
