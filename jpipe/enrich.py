"""LLM refinement: a second pass over JDs after fetch (doesn't slow down scraping).

Regex scoring is the first, coarse filter, but phrasings like
"8+ years across data engineering" slip through it. This pass runs a light
model (Haiku) over every kept job's JD and extracts structured facts:
    minimum YoE / seniority tier / sponsorship stance / US-based or not /
    summary / red flags

- Incremental: keyed on a hash of the JD body, so only new jobs or changed
  JDs trigger a call (no repeat spend).
- Results land in jobs.llm (JSON); the board shows the summary on expanded cards.
- Veto power: apply_vetoes() turns hard blockers the LLM found (YoE >=
  reject_at, explicit no-sponsorship, non-US role) into verdict='reject'.
  rescore re-derives verdicts each run, so vetoes are re-applied every time.

Two backends (profile.yaml -> llm.backend):
  - cc  : Claude Code headless (`claude -p --model haiku`) — uses your
          subscription quota, zero marginal cost. Jobs are batched (12 per
          call) to amortize startup overhead. Shares the quota pool with your
          own interactive Claude Code use.
  - api : Anthropic API (needs ANTHROPIC_API_KEY) — metered billing, roughly
          cents per day at typical volume; faster and more reliable.
  - auto (default): use api when credentials exist, else cc when the `claude`
          binary is installed, else skip enrichment.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess

from .config import profile
from .db import connect

CC_BATCH = 12          # jobs per call on the cc backend
CC_DESC_CHARS = 4000   # per-JD truncation when batching 12 at a time

MAX_DESC_CHARS = 8000

SCHEMA = {
    "type": "object",
    "properties": {
        "min_yoe": {
            "type": ["integer", "null"],
            "description": "Minimum years of experience the JD requires. When several "
                           "requirements must each be met, take the maximum; for a range "
                           "within one sentence (e.g. 2-6 years) take the lower bound; "
                           "null when not stated at all.",
        },
        "seniority": {"type": "string", "enum": ["junior", "mid", "senior"]},
        "sponsorship": {
            "type": "string", "enum": ["yes", "no", "unknown"],
            "description": "no = the JD explicitly rules out sponsorship / requires "
                           "citizenship or a green card / requires clearance; "
                           "yes = explicitly offered; otherwise unknown.",
        },
        "us_location": {
            "type": "boolean",
            "description": "Whether the role can be worked from the US (US remote counts; "
                           "purely overseas roles are false).",
        },
        "summary": {
            "type": "string",
            "description": "Two English sentences: what the role actually does + the most "
                           "important technical requirements.",
        },
        "flags": {
            "type": "array", "items": {"type": "string"},
            "description": "Red flags or highlights for the candidate, short English "
                           "phrases, at most 3 (e.g. 'requires finance background', "
                           "'stack matches exactly'); empty array when none.",
        },
    },
    "required": ["min_yoe", "seniority", "sponsorship", "us_location",
                 "summary", "flags"],
    "additionalProperties": False,
}


def _system() -> str:
    c = profile().get("candidate", {})
    return (
        "You are a structured extractor for job descriptions. Candidate context "
        "(only for judging flags): "
        f"{c.get('years_experience', 2)} years of experience, "
        f"currently {c.get('current', 'a Data Engineer')}, "
        f"visa status: {c.get('visa', 'needs sponsorship')}. "
        "Extract strictly from the JD text; never guess — use null/unknown when unsure."
    )


def _hash(desc: str) -> str:
    return hashlib.sha1((desc or "").encode()).hexdigest()[:16]


def _ensure_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    with conn:
        if "llm" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN llm TEXT")
        if "llm_hash" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN llm_hash TEXT")


def _client():
    """Return a client when credentials exist, else None (the SDK checks the
    API key env vars and the local anthropic profile in order)."""
    import os
    from pathlib import Path

    has_env = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    has_profile = (Path.home() / ".config" / "anthropic").exists()
    if not (has_env or has_profile):
        return None
    import anthropic

    return anthropic.Anthropic()


def _extract(client, model: str, job) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=600,
        system=_system(),
        messages=[{
            "role": "user",
            "content": (f"Company: {job['company']}\nTitle: {job['title']}\n"
                        f"Location: {job['location'] or 'not stated'}\n\nJob description:\n"
                        + (job["description"] or "")[:MAX_DESC_CHARS]),
        }],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def _extract_cc_batch(jobs: list) -> dict[int, dict]:
    """Claude Code headless backend: extract a batch of jobs in one call.
    Returns {index: extraction}."""
    blocks = []
    for i, j in enumerate(jobs):
        blocks.append(f"### Job {i}\nCompany: {j['company']}\nTitle: {j['title']}\n"
                      f"Location: {j['location'] or 'not stated'}\nJD:\n"
                      + (j["description"] or "")[:CC_DESC_CHARS])
    prompt = (
        _system() + "\n\nRun the extraction for every job below. Output ONLY a JSON "
        "array (no markdown code fences, no other text); each element is:\n"
        '{"idx": job index, "min_yoe": integer or null, "seniority": "junior|mid|senior", '
        '"sponsorship": "yes|no|unknown", "us_location": true/false, '
        '"summary": "two English sentences", "flags": ["short English phrase", ...]}\n\n'
        + "\n\n".join(blocks)
    )
    r = subprocess.run(
        ["claude", "-p", "--model", "haiku", "--output-format", "json", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit code {r.returncode}: {r.stderr[:200]}")
    text = json.loads(r.stdout).get("result", "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    out = {}
    for item in json.loads(text):
        if isinstance(item, dict) and isinstance(item.get("idx"), int):
            out[item.pop("idx")] = item
    return out


def llm_complete(system: str, user: str, schema: dict) -> dict:
    """One-shot structured completion on whichever backend is configured.

    Used by `jobpipe import-resume`; job enrichment keeps its own batched paths.
    """
    cfg = profile().get("llm", {})
    backend, client = _backend(cfg)
    if backend is None:
        raise SystemExit(
            "No LLM backend available: export ANTHROPIC_API_KEY=... (API billing), "
            "or install Claude Code (subscription quota), "
            "or set llm.backend in config/profile.yaml."
        )
    if backend == "api":
        resp = client.messages.create(
            model=cfg.get("model", "claude-haiku-4-5"),
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        return json.loads(next(b.text for b in resp.content if b.type == "text"))
    prompt = (
        system + "\n\nRespond with ONLY a JSON object matching this schema "
        "(no markdown code fences, no other text):\n"
        + json.dumps(schema) + "\n\n" + user
    )
    r = subprocess.run(
        ["claude", "-p", "--model", "haiku", "--output-format", "json", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit code {r.returncode}: {r.stderr[:200]}")
    text = json.loads(r.stdout).get("result", "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def apply_vetoes(conn) -> int:
    """Persist hard blockers found by the LLM as verdict='reject'.
    rescore re-derives verdicts on every run, so this must be re-entrant."""
    cfg = profile()
    reject_at = cfg.get("yoe", {}).get("reject_at", 8)
    needs_sponsor = cfg.get("candidate", {}).get("needs_sponsorship", True)
    _ensure_columns(conn)
    n = 0
    rows = conn.execute(
        "SELECT id, llm FROM jobs WHERE active=1 AND verdict='keep' AND llm IS NOT NULL"
    ).fetchall()
    with conn:
        for r in rows:
            try:
                d = json.loads(r["llm"])
            except ValueError:
                continue
            reason = None
            if d.get("min_yoe") is not None and d["min_yoe"] >= reject_at:
                reason = f"requires {d['min_yoe']}+ years of experience (LLM)"
            elif needs_sponsor and d.get("sponsorship") == "no":
                reason = "JD offers no sponsorship (LLM)"
            elif d.get("us_location") is False:
                reason = "role is outside the US (LLM)"
            if reason:
                conn.execute(
                    "UPDATE jobs SET verdict='reject', reject_reason=? WHERE id=?",
                    (reason, r["id"]))
                n += 1
    return n


def _backend(cfg: dict):
    """Return ('api', client) / ('cc', None) / (None, None)."""
    want = cfg.get("backend", "auto")
    client = _client() if want in ("auto", "api") else None
    if client is not None:
        return "api", client
    if want in ("auto", "cc") and shutil.which("claude"):
        return "cc", None
    return None, None


def _store(conn, row, data: dict) -> None:
    with conn:
        conn.execute("UPDATE jobs SET llm=?, llm_hash=? WHERE id=?",
                     (json.dumps(data, ensure_ascii=False),
                      _hash(row["description"]), row["id"]))


def run(limit: int | None = None, auto: bool = False) -> None:
    cfg = profile().get("llm", {})
    if not cfg.get("enabled", True):
        return
    backend, client = _backend(cfg)
    if backend is None:
        if not auto:
            print("No LLM backend available: either export ANTHROPIC_API_KEY=... "
                  "(API billing) or install Claude Code (subscription quota).")
        return

    model = cfg.get("model", "claude-haiku-4-5")
    cap = limit or cfg.get("max_per_run", 200)
    conn = connect()
    _ensure_columns(conn)
    rows = conn.execute(
        "SELECT id, company, title, location, description, llm_hash "
        "FROM jobs WHERE active=1 AND verdict='keep'"
    ).fetchall()
    # Skip jobs with an empty JD (adapters with a detail-fetch quota, e.g. Meta):
    # nothing to extract from. Once a later fetch fills in the body, the hash
    # changes and the job naturally enters the queue.
    todo = [r for r in rows
            if r["description"] and r["llm_hash"] != _hash(r["description"])][:cap]
    if not todo:
        if not auto:
            print("LLM refinement: no new or changed jobs.")
        conn.close()
        return

    print(f"LLM refinement: {len(todo)} jobs (backend: {backend})…")
    done = failed = 0
    if backend == "api":
        for r in todo:
            try:
                d = _extract(client, model, r)
            except Exception as e:  # one failure must not sink the round; the hash mismatch retries it next run
                failed += 1
                if failed <= 3:
                    print(f"  ! {r['company']} — {r['title'][:40]}: {type(e).__name__}: {e}")
                continue
            _store(conn, r, d)
            done += 1
    else:  # cc: batch jobs per call to amortize startup overhead
        for i in range(0, len(todo), CC_BATCH):
            batch = todo[i:i + CC_BATCH]
            try:
                results = _extract_cc_batch(batch)
            except Exception as e:
                failed += len(batch)
                print(f"  ! batch {i // CC_BATCH + 1}: {type(e).__name__}: {str(e)[:150]}")
                continue
            for idx, r in enumerate(batch):
                if idx in results:
                    _store(conn, r, results[idx])
                    done += 1
                else:
                    failed += 1
            print(f"  batch {i // CC_BATCH + 1}/{-(-len(todo) // CC_BATCH)}: {done} done so far")

    vetoed = apply_vetoes(conn)
    conn.close()
    print(f"✓ LLM refinement finished: {done} extracted"
          + (f", {failed} failed (retried next run)" if failed else "")
          + (f", {vetoed} vetoed (YoE / sponsorship / non-US blockers)" if vetoed else ""))
