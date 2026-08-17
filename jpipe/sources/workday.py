"""Workday CXS job board API (experimental).

Workday job board frontends talk to a public CXS JSON endpoint:

    POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    GET  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}

Much more brittle than Greenhouse/Lever: every company has a different
tenant/site/wdN combination, and the structure occasionally changes. If verify
fails, just remove the entry from companies.yaml — not worth agonizing over.

The `site` field in config has the format "{tenant}/{wdN}/{SiteName}",
e.g. capitalone/wd1/Capital_One
"""

from __future__ import annotations

TIMEOUT = 30
PAGE = 20
MAX_PAGES = 3  # at most 3 pages per search term — plenty
SEARCH_TERMS = ["data engineer", "machine learning engineer", "data platform"]


def _parts(entry: dict):
    site = entry.get("site") or ""
    bits = [b for b in site.split("/") if b]
    if len(bits) != 3:
        raise ValueError(f"workday site must have the form 'tenant/wdN/SiteName', got {site!r}")
    tenant, wd, name = bits
    root = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{name}"
    public = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{name}"
    return root, public


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        root, _ = _parts(entry)
    except ValueError as e:
        return False, str(e)
    try:
        r = session().post(
            f"{root}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "data"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        total = r.json().get("total")
    except Exception:
        return False, "response is not JSON"
    if total is None:
        return False, "unexpected response structure"
    return True, f"search returned {total} hits"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    root, public = _parts(entry)
    s = session()
    seen: dict[str, dict] = {}

    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            try:
                r = s.post(
                    f"{root}/jobs",
                    json={"appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": term},
                    timeout=TIMEOUT,
                )
                r.raise_for_status()
                postings = r.json().get("jobPostings") or []
            except Exception:
                break
            if not postings:
                break
            for p in postings:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen[path] = {
                    "ats_job_id": path,
                    "title": (p.get("title") or "").strip(),
                    "location": (p.get("locationsText") or "").strip(),
                    "url": public + path,
                    "department": None,
                    "posted_at": None,
                    "description": "",
                    "updated_at": None,
                    "_posted_rel": p.get("postedOn"),
                }
            if len(postings) < PAGE:
                break

    # fetch details one by one for the JD body — both keyword scoring and sponsorship detection depend on it
    for path, job in seen.items():
        try:
            d = s.get(root + path, timeout=TIMEOUT)
            if d.status_code != 200:
                continue
            info = d.json().get("jobPostingInfo") or {}
            job["description"] = clean_html(info.get("jobDescription"))
            job["posted_at"] = (info.get("startDate") or "")[:10] or None
            if info.get("externalUrl"):
                job["url"] = info["externalUrl"]
        except Exception:
            continue

    for job in seen.values():
        job.pop("_posted_rel", None)
    return list(seen.values())
