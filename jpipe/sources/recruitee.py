"""Recruitee public API.

    GET https://{token}.recruitee.com/api/offers/

The listing already includes the JD body (description + requirements).
"""

from __future__ import annotations

TIMEOUT = 30


def _url(entry: dict) -> str:
    return f"https://{entry['token']}.recruitee.com/api/offers/"


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(_url(entry), timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        n = len(r.json().get("offers") or [])
    except Exception:
        return False, "response is not JSON"
    return (n > 0), f"{n} jobs"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    r = session().get(_url(entry), timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("offers") or []:
        country = (j.get("country") or "").lower()
        remote = "remote" in (j.get("location") or "").lower()
        if country and country not in ("united states", "us", "usa") and not remote:
            continue
        out.append({
            "ats_job_id": str(j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": j.get("location") or j.get("city") or "",
            "url": j.get("careers_url"),
            "department": j.get("department"),
            "posted_at": (j.get("published_at") or j.get("created_at") or "")[:10] or None,
            "updated_at": None,
            "description": clean_html(j.get("description") or "")
                           + "\n" + clean_html(j.get("requirements") or ""),
        })
    return out
