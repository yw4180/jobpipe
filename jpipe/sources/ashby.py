"""Ashby public job board API.

    GET https://api.ashbyhq.com/posting-api/job-board/{token}
"""

from __future__ import annotations

BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}"
TIMEOUT = 25


def _url(entry: dict) -> str:
    return BASE.format(token=entry["token"])


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(_url(entry), timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code in (404, 400):
        return False, f"{r.status_code} token does not exist"
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
        if j.get("isListed") is False:
            continue
        desc = j.get("descriptionPlain") or clean_html(j.get("descriptionHtml"))
        out.append({
            "ats_job_id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "department": j.get("department") or j.get("team"),
            "posted_at": (j.get("publishedAt") or "")[:10] or None,
            "description": (desc or "").strip(),
            "updated_at": (j.get("publishedAt") or "")[:10] or None,
        })
    return out
