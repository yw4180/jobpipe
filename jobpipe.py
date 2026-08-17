#!/usr/bin/env python3
"""Job application pipeline CLI.

Day to day only two commands are needed:
    python jobpipe.py fetch      # every morning (or via cron): fetch new jobs and score them
    python jobpipe.py day        # the daily 10 minutes: sweep ghosts -> shortlist -> dashboard

See --help for the full command list.
"""

from __future__ import annotations

import argparse
import sys

from jpipe import dashboard, fetch, schedule, shortlist, sponsor, track
from jpipe.db import ALL_STATUSES, CHANNELS

ACTIVITY_KINDS = ["referral_ask", "networking", "followup", "recruiter_reply"]


def cmd_verify(a):
    fetch.verify(a.only)


def cmd_fetch(a):
    fetch.run(a.only)


def cmd_rescore(a):
    fetch.rescore()


def cmd_prune(a):
    fetch.prune()


def cmd_shortlist(a):
    shortlist.generate(n=a.n, min_score=a.min_score, show_all=a.all)


def cmd_apply(a):
    job = shortlist.resolve(a.index)
    if job is None:
        sys.exit(f"No item #{a.index} in the shortlist. Run `python jobpipe.py shortlist` first.")
    app_id = track.record_application(job, a.channel, a.resume, a.tailored, a.note)
    print(f"✓ #{app_id}  {job['company']} — {job['title']}  ({a.channel}/{a.resume})")


def cmd_manual(a):
    app_id = track.record_manual(a.company, a.title, a.url, a.channel, a.resume, a.note)
    print(f"✓ #{app_id}  {a.company} — {a.title}  ({a.channel}/{a.resume})")


def cmd_status(a):
    if track.set_status(a.id, a.status, a.note):
        print(f"✓ #{a.id} → {a.status}")
    else:
        sys.exit(f"No application #{a.id}. Use `python jobpipe.py open` to list them.")


def cmd_did(a):
    track.activity(a.kind, a.company, a.note)
    label = {"referral_ask": "asked for a referral", "networking": "networking",
             "followup": "follow-up", "recruiter_reply": "recruiter reply"}[a.kind]
    print(f"✓ Logged: {label}" + (f" — {a.company}" if a.company else ""))


def cmd_open(a):
    rows = track.open_applications()
    if not rows:
        print("No active applications.")
        return
    print(f"\n{'ID':>4}  {'Company':<22} {'Stage':<10} {'Channel':<10} {'Applied':<12} Days")
    print("  " + "─" * 74)
    for r in rows:
        print(f"{r['id']:>4}  {r['company'][:21]:<22} {r['status']:<10} "
              f"{r['channel']:<10} {r['applied']:<12} {r['age']}")
    print()


def cmd_sweep(a):
    n = track.sweep_ghosts()
    print(f"✓ {n} applications past the no-reply threshold marked as ghosted.")


def cmd_dash(a):
    path = dashboard.build(open_after=not a.no_open)
    print(f"✓ Dashboard generated: {path}")


def cmd_sponsor_ingest(a):
    n_rows, n_co = sponsor.ingest(a.path)
    print(f"✓ Read {n_rows} rows covering {n_co} companies.")
    print("  Rescoring to apply the new sponsorship data…")
    fetch.rescore()


def cmd_export(a):
    for line in track.export_records():
        print(f"✓ records/{line}")


def cmd_schedule(a):
    if a.status:
        schedule.status()
    else:
        schedule.install()


def cmd_day(a):
    """Entry point for the daily 10 minutes."""
    n = track.sweep_ghosts()
    if n:
        print(f"({n} applications past the no-reply threshold marked as ghosted)")
    shortlist.generate()
    track.export_records()
    path = dashboard.build(open_after=not a.no_open)
    print(f"\033[90mDashboard: {path}\033[0m\n")


def main():
    p = argparse.ArgumentParser(
        prog="jobpipe", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="interactive wizard: generate config/profile.yaml and config/answers.yaml")
    s.add_argument("--force", action="store_true", help="overwrite existing config without asking")
    s.set_defaults(func=lambda a: __import__("jpipe.wizard", fromlist=["run"]).run(force=a.force))

    s = sub.add_parser("import-resume",
                       help="draft profile.yaml fields from a resume PDF (requires pypdf + an LLM backend)")
    s.add_argument("path")
    s.add_argument("--yes", action="store_true", help="apply the draft without asking")
    s.set_defaults(func=lambda a: __import__("jpipe.resume_import", fromlist=["run"]).run(a.path, yes=a.yes))

    s = sub.add_parser("verify", help="probe whether the tokens in companies.yaml are valid (run this first)")
    s.add_argument("--only", help="only test companies whose name contains this string")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("fetch", help="fetch all job boards -> store -> score")
    s.add_argument("--only", help="only fetch companies whose name contains this string")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("rescore", help="re-score after editing profile.yaml (no re-fetch)")
    s.set_defaults(func=cmd_rescore)

    s = sub.add_parser("enrich", help="LLM refinement: structured extraction on new/changed JDs (requires ANTHROPIC_API_KEY)")
    s.add_argument("--limit", type=int, help="max items to process this round (default: max_per_run in profile.yaml)")
    s.set_defaults(func=lambda a: __import__("jpipe.enrich", fromlist=["run"]).run(limit=a.limit))

    s = sub.add_parser("prune", help="delete delisted stale jobs and compact the database")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("shortlist", help="generate today's application shortlist")
    s.add_argument("-n", type=int, help="how many items (default: daily_shortlist_size in profile.yaml)")
    s.add_argument("--min-score", type=int, help="minimum score")
    s.add_argument("--all", action="store_true", help="no limit, list everything")
    s.set_defaults(func=cmd_shortlist)

    s = sub.add_parser("apply", help="record an application (by shortlist index)")
    s.add_argument("index", type=int)
    s.add_argument("--channel", default="cold", choices=CHANNELS)
    s.add_argument("--resume", default="base", help="resume version label, for comparing which version performs better")
    s.add_argument("--tailored", action="store_true", help="this application was tailored")
    s.add_argument("--note")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("manual", help="record an application outside the shortlist (seen elsewhere / referral)")
    s.add_argument("--company", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--url")
    s.add_argument("--channel", default="cold", choices=CHANNELS)
    s.add_argument("--resume", default="base")
    s.add_argument("--note")
    s.set_defaults(func=cmd_manual)

    s = sub.add_parser("status", help="update the stage of an application")
    s.add_argument("id", type=int)
    s.add_argument("status", choices=ALL_STATUSES)
    s.add_argument("--note")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("did", help="log an action that is not an application (the bulk of leading indicators)")
    s.add_argument("kind", choices=ACTIVITY_KINDS)
    s.add_argument("--company")
    s.add_argument("--note")
    s.set_defaults(func=cmd_did)

    s = sub.add_parser("open", help="list active applications (to look up IDs)")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("sweep", help="mark applications past the no-reply threshold as ghosted")
    s.set_defaults(func=cmd_sweep)

    s = sub.add_parser("dash", help="generate and open the feedback dashboard")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_dash)

    s = sub.add_parser("sponsor-ingest", help="import DOL/USCIS H-1B disclosure files")
    s.add_argument("path")
    s.set_defaults(func=cmd_sponsor_ingest)

    s = sub.add_parser("export", help="export application records as CSV to records/ (this goes into git)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("schedule", help="generate a launchd job so fetch runs three times a day")
    s.add_argument("--status", action="store_true", help="only show current status")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("day", help="daily 10-minute entry point: sweep ghosts + shortlist + dashboard")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_day)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
