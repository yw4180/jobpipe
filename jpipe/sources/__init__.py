"""Job board fetchers.

Every source uses the ATS's **public** job-board API — the same endpoints the
board's own frontend calls, open and unauthenticated, so fetching them does not
violate any terms of service. Sites that prohibit automation (LinkedIn, Indeed,
and the like) are deliberately left alone.

Each source exposes two functions:
    fetch(entry) -> list[dict]   # normalized jobs
    probe(entry) -> (ok, msg)    # used by the verify command to check a token is still valid
"""

from __future__ import annotations

import html
import re

import requests

from . import (adzuna, amazon, apple, ashby, eightfold, greenhouse, hn, lever,
               meta, microsoft, oracle, recruitee, smartrecruiters, workable,
               workday)

SOURCES = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workday": workday,
    "amazon": amazon,          # amazon.jobs in-house API
    "eightfold": eightfold,    # Eightfold, the platform behind custom career sites (e.g. Netflix)
    "meta": meta,              # metacareers GraphQL
    "apple": apple,            # jobs.apple.com
    "microsoft": microsoft,    # Eightfold PCSX (not the standard eightfold API)
    "oracle": oracle,          # Oracle Cloud Recruiting (JPMorgan, banks, hospitals, universities)
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "recruitee": recruitee,
    "adzuna": adzuna,          # aggregator (free API key required); covers the long tail beyond the company list
    "hn": hn,                  # monthly HN "Who is hiring" thread
}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/html;q=0.9"})
    return s


def clean_html(raw: str | None) -> str:
    """HTML -> plain text. Job boards usually return content as escaped HTML."""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


def get(entry: dict):
    """Look up the module for entry['ats']."""
    return SOURCES.get((entry.get("ats") or "").lower())


_REL = re.compile(
    r"data engineer|analytics engineer|machine learning|ml engineer"
    r"|data platform|data infra|mlops|data scientist", re.I)  # data scientist added 2026-08-16 when the target roles were broadened


def detail_order(jobs) -> list[dict]:
    """When the listing API omits the JD and the detail quota is limited (MAX_DETAIL), put relevant titles first."""
    return sorted(jobs, key=lambda j: 0 if _REL.search(j.get("title") or "") else 1)
