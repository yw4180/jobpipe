"""Oracle Cloud Recruiting (ORC) public candidate-experience API.

Used by JPMorgan, many banks, hospitals and universities. Not guessable —
the {host}/{site} pair comes from each employer's careers page.

    list:   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
            ?finder=findReqs;siteNumber={site},keyword=...,limit=...,offset=...
    detail: GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
            ?finder=ById;Id={id},siteNumber={site}&expand=all
            → ExternalDescriptionStr holds the full JD (12k+ chars).

config site field: "{host}/{SITE}"   e.g. "jpmc.fa.oraclecloud.com/CX_1001"
"""

from __future__ import annotations

import time

TIMEOUT = 30
PAGE = 50
MAX_PAGES = 4
MAX_DETAIL = 150
SLEEP = 0.15
SEARCH_TERMS = ["data engineer", "machine learning engineer",
                "analytics engineer", "ai engineer"]


def _parts(entry: dict) -> tuple[str, str]:
    site = entry.get("site") or ""
    host, _, code = site.partition("/")
    if not host or not code:
        raise ValueError(f"oracle site must be 'host/SITE', got {site!r}")
    return host, code


def _list_url(host: str) -> str:
    return f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        host, site = _parts(entry)
    except ValueError as e:
        return False, str(e)
    try:
        r = session().get(_list_url(host), timeout=TIMEOUT, params={
            "onlyData": "true",
            "finder": f"findReqs;siteNumber={site},keyword=data engineer,limit=1"})
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        n = r.json()["items"][0].get("TotalJobsCount")
    except Exception:
        return False, "unexpected response"
    return True, f"data engineer: {n} reqs"


def _detail(s, host: str, site: str, jid: str) -> str:
    from . import clean_html

    r = s.get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
              params={"expand": "all", "onlyData": "true",
                      "finder": f"ById;Id={jid},siteNumber={site}"}, timeout=TIMEOUT)
    if not r.ok:
        return ""
    items = r.json().get("items") or []
    if not items:
        return ""
    j = items[0]
    parts = [j.get("ExternalDescriptionStr"), j.get("ExternalQualificationsStr"),
             j.get("ExternalResponsibilitiesStr")]
    return "\n".join(clean_html(p) for p in parts if p)


def fetch(entry: dict) -> list[dict]:
    from . import detail_order, session

    s = session()
    host, site = _parts(entry)
    seen: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            r = s.get(_list_url(host), timeout=TIMEOUT, params={
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": (f"findReqs;siteNumber={site},keyword={term},"
                           f"limit={PAGE},offset={page * PAGE},"
                           f"sortBy=POSTING_DATES_DESC")})
            r.raise_for_status()
            reqs = r.json()["items"][0].get("requisitionList") or []
            for j in reqs:
                jid = str(j.get("Id"))
                if jid in seen:
                    continue
                seen[jid] = {
                    "ats_job_id": jid,
                    "title": (j.get("Title") or "").strip(),
                    "location": j.get("PrimaryLocation") or "",
                    "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                           f"{site}/job/{jid}",
                    "department": j.get("JobFamily"),
                    "posted_at": (j.get("PostedDate") or "")[:10] or None,
                    "updated_at": None,
                    "description": "",
                }
            if len(reqs) < PAGE:
                break

    skip = entry.get("_skip_detail") or set()
    todo = [j for j in detail_order(seen.values()) if j["ats_job_id"] not in skip]
    for i, job in enumerate(todo):
        if i >= MAX_DETAIL:
            break
        try:
            job["description"] = _detail(s, host, site, job["ats_job_id"])
        except Exception:
            pass
        time.sleep(SLEEP)
    return list(seen.values())
