"""Meta careers site (metacareers.com).

There is no public REST API; this goes through the frontend's own GraphQL:
    1. GET /jobs with the full set of browser headers (omitting even one Sec-* header yields a 400) -> grab the LSD token
    2. POST /graphql  fb_api_req_friendly_name=CareersJobSearchResultsDataQuery
    3. the listing carries no dates/JD -> GET /jobs/{id}/ per job and parse the embedded ld+json JobPosting

DOC_ID is a frontend build artifact. When it goes stale (graphql starts erroring), re-extract it:
from the /jobs page HTML grab all rsrc.php/*.js bundles (regex rsrc\\.php\\?/[^"]+\\.js, unescaping \\/ back to /),
then search the bundles for CareersJobSearchResultsDataQuery.*?exports="(\\d+)".
"""

from __future__ import annotations

import json
import re
import time

DOC_ID = "27506805582236862"
TIMEOUT = 30
MAX_DETAIL = 150   # max detail backfills per run (relevance-matching titles first)
SLEEP = 0.4
SEARCH_TERMS = ["data engineer", "machine learning engineer"]

NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_LSD = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
_LDJSON = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _search(s, lsd: str, q: str) -> list[dict]:
    variables = {"search_input": {
        "q": q, "divisions": [], "offices": [], "roles": [],
        "leadership_levels": [], "teams": [], "sub_teams": [],
        "is_leadership": False, "is_remote_only": False,
        "sort_by_new": False, "results_per_page": None, "page": 1,
    }, "isLoggedIn": False, "viewasUserID": None}
    r = s.post("https://www.metacareers.com/graphql", timeout=TIMEOUT, data={
        "av": "0", "__user": "0", "__a": "1", "__req": "1", "dpr": "1",
        "lsd": lsd, "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery",
        "variables": json.dumps(variables), "server_timestamps": "true",
        "doc_id": DOC_ID,
    }, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.metacareers.com",
        "Referer": "https://www.metacareers.com/jobs",
        "X-Fb-Lsd": lsd, "X-Asbd-Id": "129477",
        "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })
    r.raise_for_status()
    return r.json()["data"]["job_search_with_featured_jobs"]["all_jobs"]


def _session_with_lsd():
    from . import session

    s = session()
    r = s.get("https://www.metacareers.com/jobs", headers=NAV_HEADERS,
              timeout=TIMEOUT)
    r.raise_for_status()
    m = _LSD.search(r.text)
    if not m:
        raise RuntimeError("LSD token not found in the page (anti-bot measures may have changed)")
    return s, m.group(1)


def probe(entry: dict) -> tuple[bool, str]:
    try:
        s, lsd = _session_with_lsd()
        jobs = _search(s, lsd, "data engineer")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"data engineer: {len(jobs)} hits (doc_id still valid)"


def _detail(s, job_id: str) -> dict:
    """Embedded ld+json JobPosting on the job page -> date + JD."""
    r = s.get(f"https://www.metacareers.com/jobs/{job_id}/",
              headers=NAV_HEADERS, timeout=TIMEOUT)
    if not r.ok:
        return {}
    for m in _LDJSON.finditer(r.text):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if d.get("@type") == "JobPosting":
            return d
    return {}


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, detail_order

    s, lsd = _session_with_lsd()
    seen: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for j in _search(s, lsd, term):
            jid = str(j.get("id"))
            if jid in seen:
                continue
            seen[jid] = {
                "ats_job_id": jid,
                "title": (j.get("title") or "").strip(),
                "location": "; ".join(j.get("locations") or []),
                "url": f"https://www.metacareers.com/jobs/{jid}/",
                "department": ", ".join(j.get("teams") or []) or None,
                "posted_at": None,
                "updated_at": None,
                "description": "",
            }

    skip = entry.get("_skip_detail") or set()
    todo = [j for j in detail_order(seen.values()) if j["ats_job_id"] not in skip]
    for i, job in enumerate(todo):
        if i >= MAX_DETAIL:
            break
        d = _detail(s, job["ats_job_id"])
        if d:
            job["posted_at"] = (d.get("datePosted") or "")[:10] or None
            parts = [d.get("description"), d.get("responsibilities"),
                     d.get("qualifications")]
            job["description"] = "\n".join(
                clean_html(p) for p in parts if p)
        time.sleep(SLEEP)
    return list(seen.values())
