"""Generate the daily application shortlist.

The shortlist must be prepared **in advance** of the application session:
open it and start applying, with no on-the-spot "is this worth applying to"
judgment calls.

Two outputs:
  terminal      — N ranked jobs with score breakdown and which project story to lead with
  data/today.md — same content + JD excerpts, meant to be fed to an LLM for resume tailoring
"""

from __future__ import annotations

import json
from pathlib import Path

from datetime import date, timedelta

from .config import DATA_DIR, profile, threshold
from .db import connect, today

FLAG_LABEL = {
    "positive": ("\033[32m", "JD states sponsorship"),
    "heavy":    ("\033[32m", "heavy H-1B sponsor"),
    "yes":      ("\033[36m", "has sponsorship record"),
    "unknown":  ("\033[90m", "sponsorship unknown"),
    "none":     ("\033[33m", "no H-1B record found"),
}

# Display names for location tiers, matching locations.tiers in profile.yaml
TIER_LABEL = {
    "nyc": "①NYC", "remote": "①Remote", "nj": "②NJ",
    "ca_sea": "③CA/Sea", "tx": "④TX",
}


def _stories_for(hits: list[str]) -> list[dict]:
    out = []
    hitset = {h.lower() for h in hits}
    for st in profile().get("stories") or []:
        overlap = [t for t in st.get("triggers", []) if t.lower() in hitset]
        if overlap:
            out.append({**st, "matched": overlap})
    return out


_AVAILABLE = """
    FROM jobs
    WHERE active=1 AND verdict='keep'
      AND status NOT IN ('applied','skipped')
      AND id NOT IN (SELECT job_id FROM applications WHERE job_id IS NOT NULL)
      -- Cap in-flight processes per company at concurrent_per_company,
      -- to avoid looking like a mass-application spree
      AND company NOT IN (
            SELECT company FROM applications
            WHERE status NOT IN ('rejected','ghosted','withdrawn')
            GROUP BY company HAVING COUNT(*) >= :maxco)
      -- Reposted jobs: never resurface a company+title already applied to
      AND NOT EXISTS (
            SELECT 1 FROM applications a
            WHERE a.company = jobs.company AND lower(a.title) = lower(jobs.title))
"""


def _fetch_lanes(conn, n: int, min_score: int, show_all: bool):
    """Rank by score + reserve guaranteed slots for fresh jobs.

    Why not "fresh jobs unconditionally first": the freshness bonus (+22) is
    **already baked into the score** — a hard priority on top would double
    count it, and in practice let a 55-point generic SWE role displace a
    100-point on-target DE role.

    Why reserved slots are still needed: fresh jobs expire, while high-score
    backlog jobs will still be there tomorrow. Reserving fresh_reserved slots
    ensures jobs posted within 24h are never entirely crowded out by
    high-score backlog.
    """
    f = profile().get("freshness", {})
    window = f.get("fresh_window_days", 1)
    fresh_min = f.get("fresh_lane_min_score", 55)
    reserved = f.get("fresh_reserved", 5)
    maxco = threshold("concurrent_per_company", 3)
    limit = 10_000 if show_all else n
    cutoff = (date.today() - timedelta(days=window)).isoformat()

    def is_fresh(r):
        return bool(r["posted_at"] and r["posted_at"] >= cutoff)

    ranked = conn.execute(
        f"SELECT * {_AVAILABLE} AND score >= :smin ORDER BY score DESC, posted_at DESC LIMIT :lim",
        {"smin": min_score, "maxco": maxco, "lim": limit},
    ).fetchall()
    if show_all:
        return list(ranked), {r["id"] for r in ranked if is_fresh(r)}

    picked = list(ranked)
    have_fresh = sum(1 for r in picked if is_fresh(r))
    if have_fresh < reserved:
        chosen = {r["id"] for r in picked}
        extra = [
            r for r in conn.execute(
                f"SELECT * {_AVAILABLE} AND score >= :fmin AND posted_at >= :cutoff "
                f"ORDER BY score DESC LIMIT :lim",
                {"fmin": fresh_min, "cutoff": cutoff, "maxco": maxco, "lim": reserved * 3},
            ) if r["id"] not in chosen
        ]
        # Swap out the lowest-scoring backlog jobs from the tail to make
        # room for fresh jobs
        for r in extra[: reserved - have_fresh]:
            for i in range(len(picked) - 1, -1, -1):
                if not is_fresh(picked[i]):
                    picked[i] = r
                    break
            else:
                break
        picked.sort(key=lambda x: (-x["score"], x["posted_at"] or ""))

    return picked, {r["id"] for r in picked if is_fresh(r)}


def generate(n: int | None = None, min_score: int | None = None, show_all: bool = False) -> None:
    n = n or threshold("daily_shortlist_size", 8)
    min_score = min_score if min_score is not None else threshold("shortlist_min_score", 45)

    conn = connect()
    rows, fresh_ids = _fetch_lanes(conn, n, min_score, show_all)

    if not rows:
        print("No new jobs match the criteria.")
        print("  → Run `python jobpipe.py fetch` first, or relax the threshold with --min-score.")
        conn.close()
        return

    window = profile().get("freshness", {}).get("fresh_window_days", 1)
    n_fresh = len(fresh_ids)
    print(f"\n\033[1mToday's application shortlist ({today()})\033[0m  ranked by match · "
          f"\033[32m{n_fresh} posted within {window*24}h\033[0m"
          f" · backlog {len(rows) - n_fresh}\n")

    md = [
        f"# Application shortlist · {today()}",
        "",
        "> Generated by `python jobpipe.py shortlist`. After applying, log it with "
        "`python jobpipe.py apply <index>`.",
        "",
    ]

    for i, r in enumerate(rows, 1):
        detail = json.loads(r["score_detail"] or "{}")
        parts = detail.get("parts", {})
        hits = [h for h in detail.get("hits", []) if not h.startswith("-")]
        color, flag_txt = FLAG_LABEL.get(r["sponsor_flag"], ("\033[90m", r["sponsor_flag"] or "?"))
        stories = _stories_for(hits)
        tier = detail.get("location_tier", "")
        tier_txt = TIER_LABEL.get(tier, tier)
        age = detail.get("age_days")
        if r["id"] in fresh_ids:
            age_txt = "\033[32m🔥 " + ("posted today" if not age else f"posted {age}d ago") + "\033[0m"
        else:
            age_txt = f"\033[90mposted {age}d ago\033[0m" if age is not None else "\033[90mposting date unknown\033[0m"

        print(f"\033[1m{i:>2}. [{r['score']:>3}] {r['title']}\033[0m  {age_txt}")
        print(f"     {r['company']}  ·  \033[34m{tier_txt}\033[0m {(r['location'] or 'location unlisted')[:56]}"
              f"  ·  {color}{flag_txt}\033[0m")
        yoe = detail.get("yoe") or [None, None]
        if yoe[0] is None:
            yoe_txt = "\033[90mYoE unstated\033[0m"
        else:
            rng = f"{yoe[0]}" if yoe[0] == yoe[1] else f"{yoe[0]}–{yoe[1]}"
            col = "\033[32m" if yoe[0] <= 3 else "\033[33m" if yoe[0] <= 5 else "\033[31m"
            yoe_txt = f"{col}requires {rng} yrs\033[0m"
        print(f"     {yoe_txt}  ·  score: title {parts.get('title',0)} "
              f"+ keywords {parts.get('keywords',0)} + location {parts.get('location',0)} "
              f"+ sponsor {parts.get('sponsorship',0)} + freshness {parts.get('freshness',0)} "
              f"+ yoe {parts.get('yoe',0)} {('+ junior ' + str(parts['junior_title'])) if parts.get('junior_title') else ''} "
              f"{parts.get('penalty',0) or ''}")
        if hits:
            print(f"     hits: \033[90m{', '.join(hits[:12])}\033[0m")
        if stories:
            print(f"     lead with: \033[35m{stories[0]['name']}\033[0m — {stories[0]['pitch'][:70]}")
        print(f"     {r['url']}")
        print()

        md += [
            f"## {i}. {r['title']} — {r['company']}  `score {r['score']}`" + ("  🔥 posted within 24h" if r["id"] in fresh_ids else ""),
            "",
            f"- **Location**: {tier_txt} — {r['location'] or 'unlisted'}",
            f"- **Required YoE**: " + ("unstated" if yoe[0] is None else
                (f"{yoe[0]} yrs" if yoe[0] == yoe[1] else f"{yoe[0]}–{yoe[1]} yrs")),
            f"- **Sponsorship**: {flag_txt}"
            + (f" — quote: *{detail['snippet'][:200]}*" if detail.get("snippet") else ""),
            f"- **Keyword hits**: {', '.join(hits) or '—'}",
            f"- **Suggested lead story**: " + (
                "; ".join(f"{s['name']} ({s['pitch']})" for s in stories) or "pick from the JD"
            ),
            f"- **Link**: {r['url']}",
            "",
            "<details><summary>JD excerpt</summary>",
            "",
            "```",
            (r["description"] or "")[:2500],
            "```",
            "",
            "</details>",
            "",
        ]

    md += [
        "---",
        "",
        "## Tailoring instructions for an LLM",
        "",
        "> Send this file to an LLM with a prompt like:",
        "> \"For each job's JD, pick the 3 most relevant resume bullets from the",
        "> project stories (see `stories` in config/profile.yaml), rephrase them",
        "> to match the JD's wording, and write one cover-letter opening line.",
        "> Do not invent numbers.\"",
        "",
    ]
    out = Path(DATA_DIR) / "today.md"
    out.write_text("\n".join(md), encoding="utf-8")

    with conn:
        conn.executemany(
            "UPDATE jobs SET status='shortlisted' WHERE id=? AND status='new'",
            [(r["id"],) for r in rows],
        )
    conn.close()

    print(f"\033[90mFull version (JD excerpts + tailoring instructions) written to {out.relative_to(out.parent.parent.parent)}\033[0m")
    print(f"\033[90mLog applications with: python jobpipe.py apply <index>\033[0m\n")


def resolve(index: int):
    """Resolve a shortlist index back to a job row. Must use the same dual-lane logic as generate, or apply would log the wrong job."""
    conn = connect()
    rows, _ = _fetch_lanes(
        conn, threshold("daily_shortlist_size", 8),
        threshold("shortlist_min_score", 45), show_all=False)
    conn.close()
    return rows[index - 1] if 1 <= index <= len(rows) else None
