# jobpipe

[中文文档](README.zh-CN.md)

A local-first job-search pipeline. It fetches openings from 14+ ATS job-board APIs (Greenhouse, Lever, Ashby, Workday, and more), scores every posting against your profile with a transparent rules engine, optionally refines JDs with a cheap LLM pass, hands you a ranked daily shortlist, tracks your application funnel, and serves a web board on top of the same database. Everything runs on your machine: your profile, applications, and notes live in a local SQLite file and never leave it — the only outbound data is JD text sent to the LLM backend you configure, if you enable enrichment at all.

## Quickstart

```bash
pip install -r requirements.txt

python jobpipe.py init        # interactive wizard -> config/profile.yaml + config/answers.yaml
python jobpipe.py verify      # probe the ATS tokens in config/companies.yaml
python jobpipe.py fetch       # scrape all boards, store, score (first run takes a while)
python jobpipe.py shortlist   # today's ranked application list
python board/app.py           # web board on http://127.0.0.1:5175
```

Optionally, bootstrap your profile from a resume instead of typing it in:

```bash
pip install pypdf
python jobpipe.py import-resume path/to/resume.pdf   # LLM drafts profile fields, you confirm the merge
```

## How it works

```
config/companies.yaml ─┐
                       ├─> fetch ──> jobs (SQLite) ──> score ──> enrich ──> shortlist ──> data/today.md
config/profile.yaml  ──┘                                 ^                                    │
                                              sponsor_records                                 │  you apply
                                              (H-1B data)                                     v
                                                                              applications + events
                                                                                     │
                                                                                     v
                                                                        dashboard + web board
```

1. **fetch** pulls every board in `config/companies.yaml` concurrently (8 threads) and upserts incrementally: new postings are inserted, existing ones refreshed, and anything a board stops returning is marked inactive (delisted). A company's first fetch is flagged as backlog so hundreds of old postings don't masquerade as fresh. Scoring runs immediately after, then the LLM enrichment pass runs automatically (and is silently skipped when no backend is configured).
2. **score** is a pure rules engine driven entirely by `config/profile.yaml` — no code changes to tune it, and every score is stored with a full per-component breakdown so you can audit why a job ranked where it did. The parts that earn their keep:
   - **Real YoE from the JD body, not the title.** Titles lie in both directions: some postings titled plain "Data Engineer" demand 8+ years in the body, while some "Senior" postings only ask for 3. The extractor reads the body and handles two phrasings separately — a range in one sentence ("2–6+ years of experience") takes the *lower* bound (that's the entry bar), while requirements across sentences ("10+ years in X" plus "2+ years doing Y") take the *max* (each must be met). Title-seniority penalties apply only when the body states no requirement; past `yoe.reject_at` the job is rejected outright.
   - **Word-boundary keyword matching.** Substring matching is catastrophic: `unity` hits the "equal opportunity employer" boilerplate in every JD, `ios` hits "scenarios", `ny` hits "Sunnyvale". Terms match on word boundaries; a trailing `*` means prefix match (`idempoten*` covers idempotent and idempotency).
   - **US-anchor location logic.** Any US location is kept and merely tiered for points; purely overseas roles are rejected. A posting listing "New York or London" stays; "Remote – India" does not.
   - **Sponsorship detection.** Configurable regexes catch JDs that explicitly refuse sponsorship or require citizenship/clearance (hard reject when you need sponsorship) as well as JDs that explicitly offer it (bonus), on top of company-level H-1B history (see below).
   - **Freshness from the true posted date.** `posted_at` is the primary signal; the ATS "updated" timestamp only means a recruiter touched the posting, so it earns at most a small bonus and is never treated as a new posting.
3. **enrich** (optional) runs a light LLM over new or changed JDs only (keyed on a hash of the body, so nothing is paid for twice) and extracts structured facts: minimum YoE, seniority, sponsorship stance, US-based or not, a two-sentence summary, and red flags. Hard blockers it finds — YoE at or above the reject threshold, explicit no-sponsorship, non-US role — veto the job.
4. **shortlist** writes a ranked list to the terminal and `data/today.md`. Ranking is score order plus reserved slots for fresh postings — *not* "fresh first", because the freshness bonus is already in the score and a hard priority would double-count it. Two anti-spray rules: at most `concurrent_per_company` active processes per company, and a company+title you've already applied to is never re-suggested (reposts get new IDs; without this rule they'd come back).
5. **apply / track** records each application (channel, resume version, tailored or not) and moves it through the funnel `applied → screen → tech → onsite → offer` (or `rejected` / `ghosted` / `withdrawn`). Applications silent past `ghost_days` (default 21) are swept to `ghosted` so they stop diluting your reply rates. `did` logs non-application actions (referral asks, networking, follow-ups) — the leading indicators you can actually see daily.
6. **dash / board** render the feedback loop: leading-indicator tiles, the funnel, and reply rates broken down by channel and by resume version — the table that tells you where next week's hours should go.

## Command reference

| Command | What it does |
|---|---|
| `init` | Interactive wizard: generate `config/profile.yaml` and `config/answers.yaml` |
| `import-resume <pdf>` | Draft profile fields from a resume PDF via the LLM backend (requires `pypdf`); confirm before merge |
| `verify [--only S]` | Probe whether the ATS tokens in `companies.yaml` are still valid — run this first |
| `fetch [--only S]` | Fetch all boards, upsert, score, then auto-run enrichment |
| `rescore` | Re-score everything after editing `profile.yaml` (no re-fetch) |
| `enrich [--limit N]` | LLM refinement pass over new/changed JDs |
| `prune` | Delete delisted stale jobs and compact the database |
| `shortlist [-n N] [--min-score S] [--all]` | Generate today's ranked application list |
| `apply <index> [--channel C] [--resume R] [--tailored] [--note N]` | Record an application by shortlist index |
| `manual --company C --title T [...]` | Record an application made outside the shortlist |
| `status <id> <stage> [--note N]` | Move an application through the funnel |
| `did <kind> [--company C] [--note N]` | Log a non-application action (`referral_ask`, `networking`, `followup`, `recruiter_reply`) |
| `open` | List active applications (to look up IDs) |
| `sweep` | Mark applications past the no-reply threshold as ghosted |
| `dash [--no-open]` | Generate and open the HTML feedback dashboard |
| `sponsor-ingest <file>` | Import DOL/USCIS H-1B disclosure files, then rescore |
| `export` | Export application records as CSV into `records/` |
| `schedule [--status]` | Generate a macOS launchd job so fetch runs three times a day |
| `day` | The daily entry point: sweep ghosts + shortlist + export + dashboard |

## Configuration

Three files under `config/`. Your real `profile.yaml` and `answers.yaml` are generated by `init` and **gitignored** — they contain your personal data and never leave your machine.

### profile.yaml

Every scoring rule lives here; edit it and run `rescore` (no re-fetch needed).

| Section | What it controls |
|---|---|
| `candidate` | Your name, years of experience, and `needs_sponsorship` (set `false` to disable all sponsorship logic) |
| `titles` | Target-title patterns in three tiers: `strong` (45 pts) / `good` (32) / `weak` (15); plus `title_blockers` (hard reject: staff, principal, director, intern, …), `title_penalties` (applied only when the JD body states no YoE), and `junior_title_bonus` |
| `yoe` | The bonus/penalty curve over the JD's minimum-years requirement, `reject_at` (hard reject threshold), `above_max_penalty` |
| `keywords` | Weighted stack keywords (capped by `keyword_cap`) and `keyword_penalties` for off-direction signals |
| `locations` | `tiers` (each with `match` patterns, `points`, and an optional `us_anchor` flag that keeps mixed US/overseas postings), `blockers` (overseas patterns), `default_points` |
| `sponsorship` | The `negative` / `positive` regex lists used for JD-text sponsorship detection |
| `sponsor_history_bonus`, `bar_adjust` | Points per company-level sponsor flag; adjustment for hand-labeled high-bar companies |
| `golden` | Admission rules for the board's golden page: `min_score`, `max_age_days`, `second_window_days`, `max_req_yoe`, `stretch_yoe` / `stretch_min_score`, allowed `sponsor` flags, `exclude_senior_title`, `exclude_bar`, `title_tiers` |
| `llm` | `enabled`, `backend` (`auto`/`api`/`cc`/`off`), `model`, `max_per_run` |
| `freshness` | `posted_bonus` / `updated_bonus` maps, `fresh_window_days`, `fresh_reserved` (guaranteed shortlist slots for fresh postings), `fresh_lane_min_score` |
| `thresholds` | `daily_shortlist_size`, `shortlist_min_score`, `concurrent_per_company`, `ghost_days`, `stale_days`, `archive_days` |
| `stories` | Optional project stories with trigger keywords; the shortlist and board suggest which story to lead with per job |

### answers.yaml

Your application answer sheet: an `identity` block (name, email, location, work authorization, …) and a `standard` list of recurring screening questions with your canned answers. The web board's apply mode shows these next to the posting for copy-paste.

### companies.yaml

The target-company list — ships with 350+ companies and working public ATS tokens, ready to use. Per entry: `name`, `ats`, `token` (or `site` for Workday/Eightfold-style sources), optional `sponsor` seed guess, `aliases` (legal names for H-1B matching), `bar: high` (high-bar companies get a score penalty and are excluded from the golden page), and a cosmetic `tier`. Tokens rot as companies migrate ATSes — re-run `verify` every couple of weeks and fix or drop the dead ones.

### Finding a company's ATS token

Most of the time the token is right in the careers-page URL:

| ATS | Careers URL looks like | Token / site value |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/{token}` or `job-boards.greenhouse.io/{token}` | `{token}` |
| Lever | `jobs.lever.co/{token}` | `{token}` |
| Ashby | `jobs.ashbyhq.com/{token}` | `{token}` |
| Workable | `apply.workable.com/{token}` | `{token}` |
| Recruitee | `{token}.recruitee.com` | `{token}` |
| SmartRecruiters | `careers.smartrecruiters.com/{Token}` | `{Token}` |
| Workday | `{tenant}.wd{N}.myworkdayjobs.com/{Site}` | `site: "{tenant}/wd{N}/{Site}"` |

When a company hosts the board under its own domain, use the network tab: open the careers page, press F12 → Network, refresh, and look for requests to `boards-api.greenhouse.io`, `api.lever.co`, `api.ashbyhq.com`, `apply.workable.com`, or `api.smartrecruiters.com` — the token is the path segment in that request URL. Add the entry to `config/companies.yaml` and run `verify` to confirm.

Workday is the fragile one: every company has its own tenant/site/wdN combination and the structure occasionally changes. If `verify` fails on a Workday entry, just drop it — it isn't worth fighting.

Two special sources need no token hunting: `hn` reads the monthly Hacker News "Who is hiring?" thread, and `adzuna` is an aggregator covering the long tail beyond your company list (free API key from developer.adzuna.com, via the `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` environment variables).

## LLM enrichment

Regex scoring is the coarse first filter; phrasings like "8+ years across data engineering" slip through it. The enrichment pass runs a light model (Haiku by default) over each kept job's JD and stores structured facts the scorer and board both use. Backends, set via `llm.backend` in `profile.yaml`:

- **`api`** — the Anthropic API (needs `ANTHROPIC_API_KEY`). Metered billing; at typical volume it costs roughly cents per day, and the incremental hash means a JD is never paid for twice. Faster and more reliable.
- **`cc`** — Claude Code headless (`claude -p --model haiku`). Uses your existing subscription quota, zero marginal cost; jobs are batched 12 per call to amortize startup overhead. Shares the quota pool with your own interactive use.
- **`auto`** (default) — use `api` when credentials exist, else `cc` when the `claude` binary is installed, else skip enrichment silently.
- **`off`** — disable enrichment entirely. The pipeline works fine without it; you just keep the regex-only scoring.

## H-1B sponsorship data

For candidates who need visa sponsorship — if you don't, set `candidate.needs_sponsorship: false` in `profile.yaml` and this entire subsystem switches off.

Two public, free datasets give real evidence of which companies sponsor:

- **USCIS H-1B Employer Data Hub** (approval-level, stronger evidence) — https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
- **DOL OFLC LCA disclosure files** (filing-level, larger volume) — https://www.dol.gov/agencies/eta/foreign-labor/performance (xlsx files need `pip install openpyxl`, or save as CSV first)

Download a file and ingest it:

```bash
python jobpipe.py sponsor-ingest ~/Downloads/h1b_datahubexport-2024.csv
```

Each company gets a flag from its approval count: **heavy** (25+ — a routinely sponsoring company), **yes** (3–24), **low** (1–2), **none** (no record found). The thresholds are calibrated against the actual distribution: 100+ approvals describes almost exclusively outsourcing giants, while plenty of well-known sponsoring tech companies sit in the dozens. Scoring uses the flag through `sponsor_history_bonus`, and the board's golden page admits only sponsor-positive flags by default.

One trap: **legal names are not brand names** — many companies file under a different legal entity than the brand you know. When the lookup misses, the company is misjudged as non-sponsoring, so `companies.yaml` supports an `aliases: [...]` list of legal names, and "no record found" costs only a small penalty (a false negative is worse than a miss). Until you ingest real data, the `sponsor:` seed guesses in `companies.yaml` serve as a fallback.

## Scheduling

Only `fetch` needs scheduling — everything else is interactive. On macOS:

```bash
python jobpipe.py schedule           # generates the launchd plist
python jobpipe.py schedule --status  # is it loaded? plus the latest fetch log
```

This writes `~/Library/LaunchAgents/com.jobpipe.fetch.plist` (fetch at 9:00, 13:00, and 18:00) and **prints** the `launchctl load` command for you to run — it never modifies system configuration itself. A round missed while the laptop was asleep costs nothing; postings stay up for days to weeks and the next round catches up.

## Web board

```bash
python board/app.py                  # http://127.0.0.1:5175
BOARD_PORT=8080 python board/app.py  # any other port
```

Reads the same SQLite database, no logic duplicated. Pages:

- **Golden** — the intersection worth applying to first: fresh, high-score, YoE within reach, sponsor-positive (rules in `profile.yaml` → `golden`), split into a TOP window and a catch-up window.
- **Jobs** — full browser over kept jobs with filters: search, role type, seniority, max required YoE, sponsor flag, min score, age.
- **Apply mode** — the posting side by side with your `answers.yaml` identity fields and canned screening answers for copy-paste.
- **Pipeline** — the application funnel with stage transitions.
- **Overview** — stats and reply-rate breakdowns.

The board binds to `127.0.0.1` only; it is a personal tool, not something to expose.

## Respectful use

- Every adapter calls the ATS's **public, unauthenticated job-board API** — the same endpoints the board's own frontend calls. No auth walls are bypassed, ever.
- **No LinkedIn, no Indeed.** Their terms explicitly prohibit scripted access, and detection risks the very account you're job hunting with. Postings you find there go in via `manual`.
- Be gentle: the fetcher is incremental (body text is fetched once per posting), low-volume, and rate-limits the sources that need it. Three rounds a day is the intended cadence — don't turn it into a crawler.
- This tool automates **finding and triaging**, not mass-applying. There is deliberately no auto-submit: quality of applications beats quantity, and the funnel exists to prove it to you.

## License

[MIT](LICENSE).
