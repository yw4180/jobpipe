"""SQLite layer: schema, connection, shared queries."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timedelta

from .config import DB_PATH

# Funnel ladder: higher index = further along. rejected/ghosted are terminal
# states and are not on the ladder.
FUNNEL = ["applied", "screen", "tech", "onsite", "offer"]
TERMINAL = ["rejected", "ghosted", "withdrawn"]
ALL_STATUSES = FUNNEL + TERMINAL

CHANNELS = ["cold", "referral", "recruiter", "networking", "career_fair"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,   -- sha1(company|ats_job_id)
    company       TEXT NOT NULL,
    ats           TEXT NOT NULL,
    ats_job_id    TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    department    TEXT,
    posted_at     TEXT,               -- actual publish date (greenhouse first_published, etc.)
    ats_updated   TEXT,               -- employer's last edit date; secondary freshness signal
    description   TEXT,
    first_seen    TEXT NOT NULL,      -- first time the pipeline saw it = "new to this pipeline"
    seeded        INTEGER DEFAULT 0,  -- 1 = pre-existing at initial seeding; first_seen doesn't mean truly new
    last_seen     TEXT NOT NULL,
    active        INTEGER DEFAULT 1,
    score         INTEGER,
    score_detail  TEXT,               -- JSON
    sponsor_flag  TEXT,               -- heavy|yes|unknown|none|blocked
    verdict       TEXT,               -- keep|reject
    reject_reason TEXT,
    status        TEXT DEFAULT 'new'  -- new|shortlisted|applied|skipped
);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT,
    company        TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT,
    applied_at     TEXT NOT NULL,
    channel        TEXT DEFAULT 'cold',
    resume_version TEXT DEFAULT 'base',
    tailored       INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'applied',
    status_at      TEXT NOT NULL,
    notes          TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_app_date   ON applications(applied_at);

-- Atomic log of every "what was done today" action. All leading indicators
-- are computed from this table.
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT NOT NULL,
    day            TEXT NOT NULL,     -- YYYY-MM-DD, for easy per-day aggregation
    kind           TEXT NOT NULL,     -- apply|referral_ask|recruiter_reply|networking|followup|status_change
    company        TEXT,
    application_id INTEGER,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_day ON events(day);

-- H-1B records (populated by sponsor-ingest)
CREATE TABLE IF NOT EXISTS sponsor_records (
    company_norm TEXT PRIMARY KEY,
    display_name TEXT,
    count        INTEGER NOT NULL,
    source       TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """Add new columns to an existing database (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    with conn:
        for col, ddl in [("ats_updated", "TEXT"), ("seeded", "INTEGER DEFAULT 0")]:
            if col not in have:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        # Everything fetched in the initial seeding run is pre-existing inventory;
        # its first_seen does not mean "just posted".
        if "seeded" not in have:
            conn.execute("UPDATE jobs SET seeded=1")


def job_id(company: str, ats_job_id: str) -> str:
    return hashlib.sha1(f"{company}|{ats_job_id}".encode()).hexdigest()[:16]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def log_event(conn, kind: str, company: str = None, application_id: int = None, note: str = None) -> None:
    conn.execute(
        "INSERT INTO events(at, day, kind, company, application_id, note) VALUES(?,?,?,?,?,?)",
        (now(), today(), kind, company, application_id, note),
    )


def normalize_company(name: str) -> str:
    """Collapse a company name into a comparable key, for cross-referencing H-1B data."""
    s = (name or "").lower()
    for junk in [
        " inc.", " inc", " llc", " l.l.c.", " corp.", " corp", " corporation",
        " company", " co.", " ltd.", " ltd", " limited", " holdings", " group",
        " technologies", " technology", " labs", " lab", ",", ".", "'", "-",
    ]:
        s = s.replace(junk, " ")
    return " ".join(s.split())
