# Changelog

## Unreleased

### Added
- `simplify` source: the SimplifyJobs New-Grad-Positions README (public GitHub,
  updated daily). The repo's own legend flags (no sponsorship / citizenship
  required / closed) feed the existing hard rejects; takedown cleanup for
  per-job-company aggregators is scoped by source instead of company.
- `queue` command: daily auto-apply batch from the golden-rule filter, with
  per-job resume-version matching (`queue.resume_versions` in profile.yaml)
  and a prefill kit from answers.yaml; submission results are folded back
  into the ledger on the next run. Jobs sit in a new `queued` status while
  in flight and are released if never submitted.
- `gmail-backfill` command: import past application confirmations from a
  JSON list into the ledger.
- New-grad class-year screening (`newgrad.excluded_years` in profile.yaml):
  "New Grad 2027" titles are hard-rejected for a 2024 graduate.
- ~30 AI startups (Ashby/Greenhouse boards) added to `companies.yaml`.
- `skip` command: drop a shortlist item, optionally hard-blocking its company
  (`--block-company`) or a title term (`--block-title TERM`). Blocks live in
  the gitignored `config/blocklist.yaml` and are applied as the first hard
  reject in scoring.

### Changed
- SQLite connections use a 120 s busy timeout so overlapping scheduled runs
  wait instead of failing with "database is locked".
- Example `title_blockers` now cover internship / co-op / campus program titles.
- `companies.yaml`: Temporal, Sisense and World Labs moved to Ashby; Iterable
  moved to Greenhouse; Underdog Fantasy token is now `underdog`. Marqeta,
  Instabase and Postman are commented out (boards gone, no supported ATS).

## v0.2.0 — 2026-08-18

### Added
- **Fetch-now button** in the dashboard header: runs a full fetch cycle
  (all sources → scoring → LLM refinement) with a live progress strip,
  per-phase status and ETA. Polling is self-managing — the strip picks up
  runs started by cron or another tab, and auto-hides when idle.
- `CHANGELOG.md` (this file) and version tags.

### Changed
- **Golden zone tuning**: weak-tier (generic SWE) titles are now admitted
  when score ≥ `golden.tier_stretch_min_score` (default 75). Data showed
  high-scoring junior SWE roles with sponsor history being blocked solely
  by title wording. The SWE role filter is now checked by default to match.
- Dashboard index is served with `Cache-Control: no-cache` so UI updates
  appear on plain refresh.

### Fixed
- Fetch subprocess output is unbuffered (`python -u`) so progress streams
  in real time instead of arriving in 8KB chunks.

## v0.1.0 — 2026-08-18

Initial public release: 350+ company boards (Greenhouse / Lever / Ashby /
Workday / SmartRecruiters / Workable / Recruitee), big-tech direct adapters
(Amazon / Meta / Apple / Microsoft / Netflix), aggregators (Adzuna, HN Who
is Hiring), regex pre-filter + LLM structured extraction (dual backend:
Anthropic API or Claude Code), scoring engine, local dashboard with golden
zones / apply queue / answer cards / overview analytics, `init` onboarding
wizard with resume parsing.
