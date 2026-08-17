"""Workable public widget API.

    GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true

With details=true the listing includes the JD body, so a single request covers everything.
"""

from __future__ import annotations

BASE = "https://apply.workable.com/api/v1/widget/accounts"
TIMEOUT = 30


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(f"{BASE}/{entry['token']}", params={"details": "false"},
                          timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        n = len(r.json().get("jobs") or [])
    except Exception:
        return False, "response is not JSON"
    return (n > 0), f"{n} jobs"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    r = session().get(f"{BASE}/{entry['token']}", params={"details": "true"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs") or []:
        country = (j.get("country") or "").lower()
        remote = bool(j.get("telecommuting") or j.get("remote"))
        if country and country not in ("united states", "us", "usa") and not remote:
            continue
        loc = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])) \
              or ("Remote" if remote else "")
        out.append({
            "ats_job_id": str(j.get("shortcode") or j.get("id")),
            "title": (j.get("title") or "").strip(),
            "location": loc,
            "url": j.get("url") or j.get("application_url"),
            "department": j.get("department"),
            "posted_at": (j.get("published_on") or j.get("created_at") or "")[:10] or None,
            "updated_at": None,
            "description": clean_html(j.get("description") or "")
                           + ("\n" + clean_html(j.get("requirements") or "")
                              if j.get("requirements") else ""),
        })
    return out
