"""SimplifyJobs New-Grad-Positions repo (public GitHub README, updated daily).

Source: the raw README at
https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md

Layout: one HTML <table> per ## section, columns
    Company | Role | Location | Application | Age
The Role cell carries the repo's own legend flags \u2014 free screening:
    \U0001f6c2  Does NOT offer sponsorship  -> description marker -> hard reject
                                   by the existing sponsorship regexes in score.py
    \U0001f1fa\U0001f1f8  Requires U.S. Citizenship   -> description marker -> hard reject
    \U0001f525 FAANG+ / \U0001f393 advanced degree -> informational only, kept
The Application cell shows a lock flag (\U0001f512) when the posting closed
-> row skipped. A "\u21b3" continuation row in the Company column inherits
the company above.
Age ("3d", "2mo") becomes posted_at. The README is the complete current list,
so a row that disappears between fetches was taken down (WINDOWED=False).

No JD bodies here \u2014 only title/location/link. For this source, sponsorship
screening rests on (1) the repo's own flags, (2) the H-1B sponsor_flag from
sponsor.py, and (3) the JD read at submission time.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

RAW = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"
TIMEOUT = 30
WINDOWED = False  # README is the complete current list; a missing row = taken down
AGGREGATOR = True  # per-job company names; fetch.py scopes takedown cleanup by ats

_FLAG_NO_SPONSOR = "\U0001f6c2"   # does NOT offer sponsorship
_FLAG_CITIZEN = "\U0001f1fa\U0001f1f8"  # requires U.S. citizenship
_FLAG_CLOSED = "\U0001f512"       # job application is closed
_FLAGS = "\U0001f6c2\U0001f1fa\U0001f1f8\U0001f512\U0001f525\U0001f393\U0001f4bb\U0001f4f1\U0001f916\U0001f4c8\U0001f527"

_CONT = "\u21b3"                  # continuation row: same company as above

_SECTION = re.compile(r"^##\s+(.*\S)\s*$", re.M)
_TR = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_HREF = re.compile(r'href="(https?://[^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_AGE = re.compile(r"(\d+)\s*(d|mo)\b")


def _cell_text(raw: str) -> str:
    """Visible text of a table cell: tags and legend flags dropped."""
    t = _TAG.sub("", raw)
    for f in _FLAGS:
        t = t.replace(f, "")
    return " ".join(t.split())


def _apply_url(app_raw: str) -> str | None:
    urls = _HREF.findall(app_raw)
    for u in urls:
        if "simplify.jobs" not in u:
            return u
    return urls[0] if urls else None


def parse_readme(text: str, today: date | None = None) -> list[dict]:
    today = today or date.today()
    jobs: list[dict] = []
    heads = [(m.group(1), m.start()) for m in _SECTION.finditer(text)]
    heads.append(("", len(text)))
    for (title, start), (_, end) in zip(heads, heads[1:]):
        if "role" not in title.lower():
            continue
        section = _cell_text(title)
        prev_company: str | None = None
        for rm in _TR.finditer(text[start:end]):
            row = rm.group(1)
            if "<th" in row:
                continue  # header row
            tds = _TD.findall(row)
            if len(tds) < 5:
                continue
            comp_raw, role_raw, loc_raw, app_raw, age_raw = tds[:5]
            if _FLAG_CLOSED in app_raw:
                continue  # application closed
            comp_t = _cell_text(comp_raw)
            if _CONT in comp_t:
                company = prev_company  # continuation row
            else:
                company = comp_t
            if not company:
                continue
            prev_company = company
            role_t = _cell_text(role_raw)
            if not role_t:
                continue
            markers = []
            if _FLAG_NO_SPONSOR in role_raw or _FLAG_NO_SPONSOR in comp_raw:
                # matches sponsorship negative regex: (do(es)? not|will not|cannot|unable to) sponsor
                markers.append("This posting does not sponsor.")
            if _FLAG_CITIZEN in role_raw or _FLAG_CITIZEN in comp_raw:
                # matches: u.s. citizenship (is) required
                markers.append("U.S. citizenship is required for this role.")
            description = ("[SimplifyJobs legend] " + " ".join(markers)) if markers else ""
            posted_at = None
            am = _AGE.search(_cell_text(age_raw))
            if am:
                days = int(am.group(1)) * (30 if am.group(2) == "mo" else 1)
                posted_at = (today - timedelta(days=days)).isoformat()
            loc_t = _cell_text(loc_raw)
            url = _apply_url(app_raw)
            jobs.append({
                # url is part of the id: the same company+title+location can
                # list several distinct requisitions (e.g. RTX "Software
                # Engineer 1" in one city with different req numbers)
                "ats_job_id": hashlib.sha1(
                    f"{company}|{role_t}|{loc_t}|{url}".encode()).hexdigest()[:16],
                "company": company,
                "title": role_t,
                "location": loc_t,
                "url": url,
                "department": f"SimplifyJobs \u00b7 {section}",
                "posted_at": posted_at,
                "updated_at": None,
                "description": description,
            })
    return jobs


def fetch(entry: dict) -> list[dict]:
    from . import session

    r = session().get(RAW, timeout=TIMEOUT)
    r.raise_for_status()
    return parse_readme(r.text)


def probe(entry: dict) -> tuple[bool, str]:
    from . import session

    try:
        r = session().get(RAW, timeout=TIMEOUT)
    except Exception as e:
        return False, f"network: {type(e).__name__}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    n = len(parse_readme(r.text))
    if n < 10:
        return False, f"parsed only {n} rows \u2014 layout changed?"
    return True, f"{n} new-grad rows"
