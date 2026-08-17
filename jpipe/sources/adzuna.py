"""Adzuna aggregator API — a "legal Indeed" that covers the long tail beyond the company list.

    GET https://api.adzuna.com/v1/api/jobs/us/search/{page}
        ?app_id=..&app_key=..&what=..&max_days_old=7&sort_by=date

Free signup: https://developer.adzuna.com (app_id + app_key, via the
ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables). If unset, probe reports
it and fetch returns an empty list.

Note: description is only a ~200-character snippet, so years-of-experience and
sponsorship detection are weaker here — the aggregator's role is *discovery*;
confirm promising jobs seen on the board by following the original link.
The company name is overridden per posting (supported at the fetch layer), and
sponsor records are looked up against the real employer.
"""

from __future__ import annotations

import os
import time

BASE = "https://api.adzuna.com/v1/api/jobs/us/search"
TIMEOUT = 30
WINDOWED = True  # windowed source: only the latest slice is fetched, so absent from results != delisted
PAGE = 50
MAX_PAGES = 2          # at most 100 per term (sorted newest-first; enough for a 7-day window)
SLEEP = 0.3
SEARCH_TERMS = ["data engineer", "machine learning engineer", "ai engineer",
                "analytics engineer"]


def _keys():
    return os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    app_id, app_key = _keys()
    if not (app_id and app_key):
        return False, "missing ADZUNA_APP_ID/ADZUNA_APP_KEY (free signup at developer.adzuna.com)"
    try:
        r = session().get(f"{BASE}/1", params={
            "app_id": app_id, "app_key": app_key,
            "what": "data engineer", "results_per_page": 1, "max_days_old": 7,
        }, timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    return True, f"data engineer: {r.json().get('count')} hits in the last 7 days"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    app_id, app_key = _keys()
    if not (app_id and app_key):
        return []
    s = session()
    out: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for page in range(1, MAX_PAGES + 1):
            r = s.get(f"{BASE}/{page}", params={
                "app_id": app_id, "app_key": app_key, "what": term,
                "results_per_page": PAGE, "max_days_old": 7, "sort_by": "date",
            }, timeout=TIMEOUT)
            r.raise_for_status()
            results = r.json().get("results") or []
            for j in results:
                jid = str(j.get("id"))
                if jid in out:
                    continue
                out[jid] = {
                    "ats_job_id": jid,
                    "company": ((j.get("company") or {}).get("display_name")
                                or "Unknown company (Adzuna)"),
                    "title": (j.get("title") or "").strip(),
                    "location": (j.get("location") or {}).get("display_name") or "",
                    "url": j.get("redirect_url"),
                    "department": (j.get("category") or {}).get("label"),
                    "posted_at": (j.get("created") or "")[:10] or None,
                    "updated_at": None,
                    "description": clean_html(j.get("description") or ""),
                }
            if len(results) < PAGE:
                break
            time.sleep(SLEEP)
    return list(out.values())
