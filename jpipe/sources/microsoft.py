"""Microsoft careers site.

The old gcsservices.careers.microsoft.com is retired (even its certificate was
replaced); the whole site now runs on Eightfold's PCSX platform, but it is
**not** the standard eightfold API (/api/apply/v2/jobs returns 403 on this
domain) — the pcsx endpoints must be used instead:

    GET https://apply.careers.microsoft.com/api/pcsx/search
        ?domain=microsoft.com&query=...&start=N&sort_by=timestamp   <- fixed 10 per page
    GET https://apply.careers.microsoft.com/api/pcsx/position_details
        ?position_id={id}&domain=microsoft.com&hl=en                <- JD body
"""

from __future__ import annotations

import time
from datetime import date

BASE = "https://apply.careers.microsoft.com/api/pcsx"
TIMEOUT = 30
WINDOWED = True  # windowed source: only the latest slice is fetched, so absent from results != delisted
PAGE = 10
MAX_PAGES = 10     # newest 100 per term
MAX_DETAIL = 150
SLEEP = 0.25
SEARCH_TERMS = ["data engineer", "machine learning engineer"]


def _epoch_date(v) -> str | None:
    try:
        return date.fromtimestamp(int(v)).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _search(s, term: str, start: int) -> dict:
    r = s.get(f"{BASE}/search", timeout=TIMEOUT,
              params={"domain": "microsoft.com", "query": term,
                      "location": "", "start": start, "sort_by": "timestamp"})
    r.raise_for_status()
    return r.json().get("data") or {}


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        d = _search(session(), "data engineer", 0)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if not d.get("positions"):
        return False, "positions is empty (the endpoint may have moved again)"
    return True, f"data engineer: {d.get('count')} hits"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, detail_order, session

    s = session()
    seen: dict[str, dict] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            d = _search(s, term, page * PAGE)
            positions = d.get("positions") or []
            for p in positions:
                pid = str(p.get("id"))
                if pid in seen:
                    continue
                locs = p.get("locations") or []
                seen[pid] = {
                    "ats_job_id": pid,
                    "title": (p.get("name") or "").strip(),
                    "location": "; ".join(locs) if isinstance(locs, list)
                                else str(locs),
                    "url": f"https://apply.careers.microsoft.com/careers/job/{pid}",
                    "department": p.get("department"),
                    "posted_at": _epoch_date(p.get("postedTs")
                                             or p.get("creationTs")),
                    "updated_at": None,
                    "description": "",
                }
            if len(positions) < PAGE:
                break

    skip = entry.get("_skip_detail") or set()
    todo = [j for j in detail_order(seen.values()) if j["ats_job_id"] not in skip]
    for i, job in enumerate(todo):
        if i >= MAX_DETAIL:
            break
        try:
            r = s.get(f"{BASE}/position_details", timeout=TIMEOUT,
                      params={"position_id": job["ats_job_id"],
                              "domain": "microsoft.com", "hl": "en"})
            if r.ok:
                d = (r.json().get("data") or {})
                job["description"] = clean_html(d.get("jobDescription"))
                if d.get("publicUrl"):
                    job["url"] = d["publicUrl"]
        except Exception:
            pass
        time.sleep(SLEEP)
    return list(seen.values())
