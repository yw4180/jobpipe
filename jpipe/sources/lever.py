"""Lever public job board API.

    GET https://api.lever.co/v0/postings/{token}?mode=json
"""

from __future__ import annotations

from datetime import datetime, timezone

BASE = "https://api.lever.co/v0/postings/{token}?mode=json"
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
        data = r.json()
    except Exception:
        return False, "response is not JSON"
    if not isinstance(data, list):
        return False, "unexpected response structure"
    return True, f"{len(data)} jobs"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    r = session().get(_url(entry), timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        posted = None
        if ts:
            try:
                posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
            except (TypeError, ValueError, OSError):
                posted = None
        desc = j.get("descriptionPlain") or clean_html(j.get("description"))
        # lists = the requirements/responsibilities blocks; appending them to the body makes keyword matching more accurate
        for blk in j.get("lists") or []:
            desc += "\n" + (blk.get("text") or "") + "\n" + clean_html(blk.get("content"))
        out.append({
            "ats_job_id": str(j.get("id")),
            "title": (j.get("text") or "").strip(),
            "location": (cat.get("location") or "").strip(),
            "url": j.get("hostedUrl"),
            "department": cat.get("team") or cat.get("department"),
            "posted_at": posted,
            "description": desc.strip(),
            "updated_at": posted,
        })
    return out
