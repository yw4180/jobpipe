"""Job scoring + sponsorship text detection.

All rules come from config/profile.yaml; this code only executes them.
Output (verdict, score, detail):
    verdict = 'keep' | 'reject'
    Reject reasons are stored in jobs.reject_reason so the rules can be
    audited afterwards in case they are too aggressive.
"""

from __future__ import annotations

import functools
import re
from datetime import date

from .config import profile

TITLE_POINTS = {"strong": 45, "good": 32, "weak": 15}


@functools.lru_cache(maxsize=None)
def term(t: str) -> re.Pattern:
    """Compile a config term into a **word-boundary** regex.

    Plain substring matching produces catastrophic false positives: 'unity'
    matches the "equal opportunity employer" boilerplate found in every JD,
    'ios' matches "scenarios", 'ny' matches "Sunnyvale".
    A term ending in * means prefix match (e.g. 'idempoten*' covers both
    idempotent and idempotency).
    """
    prefix = t.endswith("*")
    core = t[:-1] if prefix else t
    left = r"(?<![a-z0-9])" if core[:1].isalnum() else ""
    right = "" if prefix else (r"(?![a-z0-9])" if core[-1:].isalnum() else "")
    return re.compile(left + re.escape(core) + right, re.I)


def has(text: str, t: str) -> bool:
    return bool(term(t).search(text))


@functools.lru_cache(maxsize=None)
def _compiled(kind: str) -> list[re.Pattern]:
    pats = profile().get("sponsorship", {}).get(kind, [])
    return [re.compile(p, re.I) for p in pats]


def sponsorship_signal(text: str) -> tuple[str | None, str | None]:
    """Return (signal, matched source snippet). signal in {'blocked','positive',None}"""
    for pat in _compiled("negative"):
        m = pat.search(text)
        if m:
            lo, hi = max(0, m.start() - 60), min(len(text), m.end() + 60)
            return "blocked", text[lo:hi].replace("\n", " ").strip()
    for pat in _compiled("positive"):
        m = pat.search(text)
        if m:
            lo, hi = max(0, m.start() - 60), min(len(text), m.end() + 60)
            return "positive", text[lo:hi].replace("\n", " ").strip()
    return None, None


def _title_tier(title_l: str) -> str | None:
    titles = profile().get("titles", {})
    for tier in ("strong", "good", "weak"):
        for pat in titles.get(tier) or []:
            if has(title_l, pat):
                return tier
    return None


def _location_tier(loc_l: str) -> dict | None:
    """Return the highest-scoring location tier matched, or None (falls back to default_points)."""
    best = None
    for tier in profile().get("locations", {}).get("tiers") or []:
        for pat in tier.get("match") or []:
            if has(loc_l, pat):
                if best is None or tier.get("points", 0) > best.get("points", 0):
                    best = tier
                break
    return best


# Must catch "5+ years", "3-5 years", "4 to 6 years", "5–8 years"
_YOE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to|or)\s*(\d{1,2})\s*\+?)?\s*"
    r"(?:\+\s*)?years?\b",
    re.I,
)
# Only accept "years of experience" contexts — don't count "5 years of runway",
# "2 years ago", etc.
_YOE_CTX = re.compile(r"experien|professional|industry|working|hands[- ]on|background", re.I)
# Supplementary context: phrasings like "8+ years across data engineering" and
# "3+ years building X" never contain the word "experience"; they are detected
# by a preposition/gerund directly following "years".
# "ago" is not an -ing word so it is naturally excluded; runway/funding are
# known noise handled separately.
_YOE_AFTER = re.compile(r"^\s*['’]?s?\s*(?:of|in|across|with|[a-z]+ing\b)", re.I)
_YOE_NOISE = re.compile(r"^\s*(?:['’]?s?\s*)?of\s+(?:runway|funding|operating)", re.I)


def required_yoe(desc: str) -> tuple[int | None, int | None]:
    """Extract required years of experience from the JD body, returning (min, max).

    Why the title alone is not enough: in practice some postings titled
    "Data Engineer" (no Senior) demand 8+ years in the body, while some
    "Senior Data Engineer" postings only ask for 3. **The body requirement
    is the real bar.**

    Extraction rules (tuned empirically — the two phrasings must be handled
    separately):
      - **Range within one sentence**: "2-6+ years of experience" → take the
        lower bound 2; that is the entry bar.
      - **Across different sentences**: "10+ years in Data Engineering" +
        "2+ years deploying AI" → take the max, 10, because every requirement
        must be satisfied.

    Taking only the min would misjudge a Lead role (with the 10+ year / 2+
    year pair of sentences) as requiring 2 years; taking only the max would
    misjudge a single "2-6 years" range as requiring 6 years.
    """
    if not desc:
        return None, None
    lows: list[int] = []   # entry bar of each sentence
    spans: list[int] = []  # every number seen, for display only
    for m in _YOE.finditer(desc):
        # Look at surrounding text to confirm this is about years of
        # experience, not something like "80 hours per year"
        start = max(0, m.start() - 120)
        after = desc[m.end():m.end() + 40]
        if not (_YOE_CTX.search(desc[start:m.end() + 120])
                or (_YOE_AFTER.match(after) and not _YOE_NOISE.match(after))):
            continue
        nums = [int(g) for g in m.groups() if g and 0 < int(g) <= 20]
        if not nums:
            continue
        lows.append(min(nums))
        spans.extend(nums)
    if not lows:
        return None, None
    return max(lows), max(spans)


def _age_days(datestr: str | None) -> int | None:
    if not datestr:
        return None
    try:
        return (date.today() - date.fromisoformat(str(datestr)[:10])).days
    except ValueError:
        return None


def freshness(job: dict) -> tuple[int, int | None]:
    """Return (freshness bonus, days since posting).

    posted_at is the true publication date — the primary signal. ats_updated
    only means the recruiter touched the posting recently — weak evidence
    (the opening is still alive), so it only adds a small bonus when
    posted_at is no longer fresh; it must never be treated as a new posting.
    """
    f = profile().get("freshness", {})
    posted = _age_days(job.get("posted_at"))
    bonus = 0
    if posted is not None:
        for days, pts in sorted((f.get("posted_bonus") or {}).items()):
            if posted <= int(days):
                bonus = pts
                break
    if bonus == 0:
        upd = _age_days(job.get("ats_updated"))
        if upd is not None:
            for days, pts in sorted((f.get("updated_bonus") or {}).items()):
                if upd <= int(days):
                    bonus = pts
                    break
    return bonus, posted


def score_job(job: dict, company_sponsor: str = "unknown",
              company_bar: str | None = None) -> tuple[str, int, dict]:
    p = profile()
    title_l = (job.get("title") or "").lower()
    loc_l = (job.get("location") or "").lower()
    desc = job.get("description") or ""
    blob = f"{title_l}\n{loc_l}\n{desc}".lower()

    detail: dict = {"parts": {}, "hits": []}

    # ── Hard reject 1: title seniority/direction mismatch ─────────
    for bad in p.get("title_blockers") or []:
        if has(title_l, bad):
            return "reject", 0, {**detail, "reason": f"Title contains '{bad.strip()}'"}

    # ── Hard reject 2: purely overseas location ───────────────────
    # Any US location is kept — the goal is opportunity volume; only
    # non-sponsorable overseas roles are dropped.
    # Matching a us_anchor tier keeps the job: "New York or London" stays,
    # "Remote - India" does not.
    loc_tier = _location_tier(loc_l)
    if not (loc_tier and loc_tier.get("us_anchor")):
        for bad in (p.get("locations", {}).get("blockers") or []):
            if has(loc_l, bad):
                return "reject", 0, {**detail, "reason": f"Overseas role '{job.get('location')}'"}

    # ── Hard reject 3: JD explicitly refuses sponsorship / requires citizenship or clearance ──
    # Only applies when the candidate needs sponsorship
    # (candidate.needs_sponsorship, default true)
    needs_sponsor = p.get("candidate", {}).get("needs_sponsorship", True)
    signal, snippet = sponsorship_signal(desc)
    if needs_sponsor and signal == "blocked":
        return "reject", 0, {**detail, "reason": "JD offers no sponsorship", "snippet": snippet}

    # ── Title score (dominant component) ──────────────────────────
    tier = _title_tier(title_l)
    if tier is None:
        return "reject", 0, {**detail, "reason": "Title not in target directions"}
    detail["parts"]["title"] = TITLE_POINTS[tier]
    detail["title_tier"] = tier
    # Store the senior flag separately: both the sweet-spot band and the
    # seniority filter use it (word boundary; senior/sr both count)
    detail["senior_title"] = 1 if (has(title_l, "senior") or has(title_l, "sr")) else 0

    # ── YoE requirement (body first, title only as fallback) ──────
    yoe_cfg = p.get("yoe", {})
    lo, hi = required_yoe(desc)
    detail["yoe"] = [lo, hi]

    if lo is not None and lo >= yoe_cfg.get("reject_at", 8):
        return "reject", 0, {**detail, "reason": f"Requires {lo}+ years of experience"}

    yoe_pts = 0
    if lo is not None:
        for years, pts in sorted((yoe_cfg.get("bonus") or {}).items()):
            if lo <= int(years):
                yoe_pts = pts
                break
        else:
            yoe_pts = yoe_cfg.get("above_max_penalty", -18)
    detail["parts"]["yoe"] = yoe_pts

    # junior/entry signals in the title — especially useful when the body
    # states no YoE requirement
    jbonus = 0
    for word, w in (p.get("junior_title_bonus") or {}).items():
        if has(title_l, word):
            jbonus = max(jbonus, w)
            detail["hits"].append(f"+{word}")
    detail["parts"]["junior_title"] = jbonus

    # Title seniority penalties apply **only when the body states no YoE** —
    # when the body does, the body wins. Applying both would wrongly kill
    # jobs like "Senior ... 3+ years" that are actually within reach.
    tpen = 0
    if lo is None:
        for word, w in (p.get("title_penalties") or {}).items():
            if has(title_l, word):
                tpen += w
                detail["hits"].append(f"-{word}")
    detail["parts"]["title_penalty"] = -tpen

    # ── Keyword score ─────────────────────────────────────────────
    kw_score = 0
    for kw, w in (p.get("keywords") or {}).items():
        if has(blob, kw):
            kw_score += w
            detail["hits"].append(kw.rstrip("*"))
    kw_score = min(kw_score, p.get("keyword_cap", 30))
    detail["parts"]["keywords"] = kw_score

    penalty = 0
    for kw, w in (p.get("keyword_penalties") or {}).items():
        if has(blob, kw):
            penalty += w
            detail["hits"].append(f"-{kw}")
    detail["parts"]["penalty"] = -penalty

    # ── Location score ────────────────────────────────────────────
    locs = p.get("locations", {})
    detail["parts"]["location"] = (
        loc_tier["points"] if loc_tier else locs.get("default_points", 0)
    )
    detail["location_tier"] = loc_tier["name"] if loc_tier else "Other US location"

    # ── Sponsorship score ─────────────────────────────────────────
    sp = 0
    if needs_sponsor:
        if signal == "positive":
            sp += 10
            detail["snippet"] = snippet
        sp += (p.get("sponsor_history_bonus") or {}).get(company_sponsor, 0)
    detail["parts"]["sponsorship"] = sp
    detail["sponsor_flag"] = "positive" if signal == "positive" else company_sponsor

    # ── Company bar score (hand-labeled in companies.yaml, see profile.yaml notes) ──
    bar = company_bar or "mid"
    detail["parts"]["bar"] = (p.get("bar_adjust") or {}).get(bar, 0)
    detail["bar"] = bar

    # ── Freshness score ───────────────────────────────────────────
    fresh_pts, age = freshness(job)
    detail["parts"]["freshness"] = fresh_pts
    detail["age_days"] = age

    total = sum(detail["parts"].values())
    return "keep", max(0, min(100, total)), detail
