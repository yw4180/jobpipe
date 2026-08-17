"""SmartRecruiters public job board API.

    GET https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
    GET https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}   <- JD body

The listing mixes jobs worldwide; filter client-side on location.country=='us' (or remote).
"""

from __future__ import annotations

import time

BASE = "https://api.smartrecruiters.com/v1/companies"
TIMEOUT = 30
PAGE = 100
MAX_PAGES = 10
MAX_DETAIL = 150
SLEEP = 0.15


def _url(entry: dict) -> str:
    return f"{BASE}/{entry['token']}/postings"


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(_url(entry), params={"limit": 1}, timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        n = r.json().get("totalFound")
    except Exception:
        return False, "response is not JSON"
    return True, f"{n} jobs (worldwide)"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, detail_order, session

    s = session()
    out: dict[str, dict] = {}
    for page in range(MAX_PAGES):
        r = s.get(_url(entry), params={"limit": PAGE, "offset": page * PAGE},
                  timeout=TIMEOUT)
        r.raise_for_status()
        items = r.json().get("content") or []
        for j in items:
            loc = j.get("location") or {}
            if loc.get("country", "").lower() != "us" and not loc.get("remote"):
                continue
            jid = str(j.get("id"))
            out[jid] = {
                "ats_job_id": jid,
                "title": (j.get("name") or "").strip(),
                "location": loc.get("fullLocation")
                            or ", ".join(filter(None, [loc.get("city"), loc.get("region")]))
                            or ("Remote" if loc.get("remote") else ""),
                "url": f"https://jobs.smartrecruiters.com/{entry['token']}/{jid}",
                "department": (j.get("function") or {}).get("label"),
                "posted_at": (j.get("releasedDate") or "")[:10] or None,
                "updated_at": None,
                "description": "",
            }
        if len(items) < PAGE:
            break

    skip = entry.get("_skip_detail") or set()
    todo = [j for j in detail_order(out.values()) if j["ats_job_id"] not in skip]
    for i, job in enumerate(todo):
        if i >= MAX_DETAIL:
            break
        try:
            r = s.get(f"{_url(entry)}/{job['ats_job_id']}", timeout=TIMEOUT)
            if r.ok:
                secs = (r.json().get("jobAd") or {}).get("sections") or {}
                job["description"] = "\n".join(
                    clean_html(secs[k].get("text")) for k in
                    ("jobDescription", "qualifications", "additionalInformation")
                    if isinstance(secs.get(k), dict) and secs[k].get("text"))
        except Exception:
            pass
        time.sleep(SLEEP)
    return list(out.values())
