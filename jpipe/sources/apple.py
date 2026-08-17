"""Apple careers site (jobs.apple.com) public search API.

    GET  /api/v1/CSRFToken            <- token is in the X-Apple-CSRF-Token response header (case-sensitive)
    POST /api/v1/search               <- fixed 20 per page; the listing includes the post date
    GET  /api/v1/jobDetails/{id}      <- full JD + minimumQualifications (years of experience live here)
"""

from __future__ import annotations

import time

BASE = "https://jobs.apple.com/api/v1"
TIMEOUT = 30
WINDOWED = True  # windowed source: only the latest slice is fetched, so absent from results != delisted
PAGE = 20
MAX_PAGES = 5      # newest 100 per term — enough to cover new postings
MAX_DETAIL = 150
SLEEP = 0.25
SEARCH_TERMS = ["data engineer", "machine learning engineer"]


def _csrf(s) -> str:
    r = s.get(f"{BASE}/CSRFToken", timeout=TIMEOUT)
    return r.headers.get("X-Apple-CSRF-Token", "")


def _search(s, token: str, term: str, page: int) -> dict:
    r = s.post(f"{BASE}/search", timeout=TIMEOUT,
               headers={"X-Apple-CSRF-Token": token} if token else {},
               json={"query": term,
                     "filters": {"locations": ["postLocation-USA"]},
                     "page": page, "locale": "en-us", "sort": "newest",
                     "format": {"longDate": "MMMM D, YYYY",
                                "mediumDate": "MMM D, YYYY"}})
    r.raise_for_status()
    return r.json().get("res") or {}


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        s = session()
        res = _search(s, _csrf(s), "data engineer", 1)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    n = res.get("totalRecords")
    if not res.get("searchResults"):
        return False, f"searchResults is empty (total={n}; the payload shape may have changed)"
    return True, f"data engineer: {n} hits"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, detail_order, session

    s = session()
    token = _csrf(s)
    seen: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for page in range(1, MAX_PAGES + 1):
            res = _search(s, token, term, page)
            results = res.get("searchResults") or []
            for j in results:
                jid = str(j.get("positionId") or j.get("id"))
                if jid in seen:
                    continue
                locs = j.get("locations") or []
                seen[jid] = {
                    "ats_job_id": jid,
                    "title": (j.get("postingTitle") or "").strip(),
                    "location": "; ".join(
                        (l.get("name") or "") for l in locs if isinstance(l, dict)),
                    "url": f"https://jobs.apple.com/en-us/details/{jid}",
                    "department": j.get("team", {}).get("teamName")
                                  if isinstance(j.get("team"), dict) else None,
                    "posted_at": (j.get("postDateInGMT") or "")[:10] or None,
                    "updated_at": None,
                    "description": clean_html(j.get("jobSummary")),
                }
            if len(results) < PAGE:
                break

    # The full JD (notably the years-of-experience requirements in
    # minimumQualifications) lives in the detail endpoint.
    # Skipped jobs get an empty body: the database already holds the full JD,
    # and this round's short listing snippet must not be carried back over it.
    skip = entry.get("_skip_detail") or set()
    for j in seen.values():
        if j["ats_job_id"] in skip:
            j["description"] = ""
    todo = [j for j in detail_order(seen.values()) if j["ats_job_id"] not in skip]
    for i, job in enumerate(todo):
        if i >= MAX_DETAIL:
            break
        try:
            r = s.get(f"{BASE}/jobDetails/{job['ats_job_id']}",
                      params={"locale": "en-us"}, timeout=TIMEOUT,
                      headers={"X-Apple-CSRF-Token": token} if token else {})
            if r.ok:
                d = r.json().get("res") or {}
                parts = [d.get("jobSummary"), d.get("description"),
                         d.get("minimumQualifications"),
                         d.get("preferredQualifications")]
                text = "\n".join(clean_html(p) for p in parts if p)
                if text:
                    job["description"] = text
        except Exception:
            pass
        time.sleep(SLEEP)
    return list(seen.values())
