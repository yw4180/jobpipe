"""Hacker News monthly "Ask HN: Who is hiring?" thread (public Algolia API).

    1. search_by_date finds the two most recent hiring threads
    2. items/{id} returns all top-level comments
    3. keyword-filter for data/ML/AI posts, then parse the company name by
       convention (first line is customarily "Company | Role | Location | ...")

This is a source LinkedIn does not have: startups hiring directly, often
stating their visa policy explicitly. Posts are unstructured text, so the body
goes to the LLM refinement pass to extract years-of-experience / sponsorship —
a perfect complement.
"""

from __future__ import annotations

import re

TIMEOUT = 30
WINDOWED = True  # windowed source: only the latest slice is fetched, so absent from results != delisted
SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{}"
# keep a top-level comment only if it mentions one of these (case-insensitive)
RELEVANT = re.compile(
    r"data engineer|analytics engineer|machine learning|ml engineer|mlops"
    r"|ai engineer|llm|data platform|data infra|data scientist", re.I)


def _threads(s, n: int = 2) -> list[dict]:
    r = s.get(SEARCH, params={
        "tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?",
        "hitsPerPage": 6,
    }, timeout=TIMEOUT)
    r.raise_for_status()
    hits = [h for h in r.json().get("hits", [])
            if "who is hiring" in (h.get("title") or "").lower()]
    return hits[:n]


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        th = _threads(session(), 1)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if not th:
        return False, "hiring thread not found"
    return True, f"latest thread: {th[0].get('title')}"


def _company(text: str) -> str:
    """First line is customarily "Company | Role | Location"; take the first segment as the company name."""
    first = text.split("\n", 1)[0]
    name = re.split(r"\||–|—", first, 1)[0].strip()
    name = re.sub(r"\(.*?\)", "", name).strip(" .,:;-")
    return name[:60] if 2 <= len(name) <= 60 else "HN post"


def fetch(entry: dict) -> list[dict]:
    from . import clean_html, session

    s = session()
    out = []
    for th in _threads(s):
        r = s.get(ITEM.format(th["objectID"]), timeout=TIMEOUT)
        if not r.ok:
            continue
        for c in r.json().get("children") or []:
            text = clean_html(c.get("text") or "")
            if not text or not RELEVANT.search(text):
                continue
            cid = str(c.get("id"))
            out.append({
                "ats_job_id": cid,
                "company": _company(text),
                "title": text.split("\n", 1)[0][:120],  # first line serves as the title so scoring runs as usual
                "location": "",  # the location is buried in the body; the LLM decides us_location
                "url": f"https://news.ycombinator.com/item?id={cid}",
                "department": "HN Who is Hiring",
                "posted_at": (c.get("created_at") or "")[:10] or None,
                "updated_at": None,
                "description": text,
            })
    return out
