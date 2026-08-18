# Changelog

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
