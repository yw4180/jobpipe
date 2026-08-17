"""Generate a macOS launchd job so fetch runs three times a day.

Why launchd instead of a server: fetch is the only step that needs scheduling, and
a missed round costs nothing (the next round catches up; jobs stay posted for days
to weeks). The applications table is a private application log that is simplest to
keep local — a server would mean handling deployment, secrets, and delivering the
dashboard back to the user. See the README section on running on a server.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import ROOT

LABEL = "com.jobpipe.fetch"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

# The three fetch times: before the workday, early afternoon, evening.
# Run `jobpipe.py day` after a fetch.
HOURS = [9, 13, 18]

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>fetch</string>
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>
  <!-- A round missed while the laptop was asleep is run once after waking -->
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{errlog}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin:{home}/.local/bin</string></dict>
</dict>
</plist>
"""


def install(dry_run: bool = True) -> None:
    logdir = ROOT / "data"
    logdir.mkdir(exist_ok=True)
    intervals = "\n".join(
        f"    <dict><key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>0</integer></dict>"
        for h in HOURS
    )
    plist = TEMPLATE.format(
        label=LABEL,
        python=sys.executable,
        script=str(ROOT / "jobpipe.py"),
        cwd=str(ROOT),
        home=str(Path.home()),
        intervals=intervals,
        log=str(logdir / "fetch.log"),
        errlog=str(logdir / "fetch.err.log"),
    )

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Only write the plist and print the load command; never run `launchctl load`
    # here — changing system configuration is left to the user.
    PLIST_PATH.write_text(plist)
    print(f"✓ Wrote {PLIST_PATH}")
    print(f"  Fetch times: daily at {', '.join(f'{h}:00' for h in HOURS)}")
    print(f"  Logs: {logdir / 'fetch.log'}")
    print()
    print("\033[1mRun this command to enable it (this tool never modifies system configuration itself):\033[0m")
    print(f"  launchctl unload {PLIST_PATH} 2>/dev/null; launchctl load {PLIST_PATH}")
    print()
    print("To disable:")
    print(f"  launchctl unload {PLIST_PATH}")
    print("To check whether it is running:")
    print(f"  launchctl list | grep {LABEL}")


def status() -> None:
    print(f"plist: {PLIST_PATH}  {'exists' if PLIST_PATH.exists() else 'missing'}")
    os.system(f"launchctl list | grep {LABEL} || echo '  not loaded in launchd'")
    log = ROOT / "data" / "fetch.log"
    if log.exists():
        print(f"\nTail of the latest fetch log ({log}):")
        os.system(f"tail -6 '{log}'")
