"""Amazon.jobs public search API.

    GET https://www.amazon.jobs/en/search.json?base_query=...&country=USA

Amazon runs its own careers site (no third-party ATS). The listing response
already includes the full JD plus basic/preferred qualifications (the
years-of-experience requirements are all written in the quals, which is key for
yoe detection). The job volume is huge (50k+ US-wide), so fetching has to go
through search terms — a full crawl is not feasible.
"""

from __future__ import annotations

from datetime import datetime

BASE = "https://www.amazon.jobs/en/search.json"
TIMEOUT = 30
WINDOWED = True  # windowed source: only the latest slice is fetched, so absent from results != delisted
PAGE = 100
MAX_PAGES = 4  # at most 400 per term, sorted by recent — enough to cover new postings
SEARCH_TERMS = ["data engineer", "machine learning engineer",
                "business intelligence engineer"]


def _params(term: str, offset: int) -> dict:
    return {"base_query": term, "country": "USA", "sort": "recent",
            "result_limit": PAGE, "offset": offset}


def _date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(BASE, params=_params("data engineer", 0), timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        hits = r.json().get("hits")
    except Exception:
        return False, "response is not JSON"
    return True, f"data engineer: {hits} hits"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    s = session()
    seen: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            r = s.get(BASE, params=_params(term, page * PAGE), timeout=TIMEOUT)
            r.raise_for_status()
            jobs = r.json().get("jobs") or []
            for j in jobs:
                jid = str(j.get("id_icims") or j.get("id"))
                if jid in seen:
                    continue
                desc = "\n".join(clean_html(j.get(k)) for k in
                                 ("description", "basic_qualifications",
                                  "preferred_qualifications") if j.get(k))
                seen[jid] = {
                    "ats_job_id": jid,
                    "title": (j.get("title") or "").strip(),
                    "location": (j.get("normalized_location")
                                 or j.get("location") or "").strip(),
                    "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                    "department": j.get("job_category") or j.get("business_category"),
                    "posted_at": _date(j.get("posted_date")),
                    "updated_at": None,
                    "description": desc,
                }
            if len(jobs) < PAGE:
                break
    return list(seen.values())
