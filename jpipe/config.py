"""Configuration and paths."""

from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROFILE_PATH = CONFIG_DIR / "profile.yaml"
ANSWERS_PATH = CONFIG_DIR / "answers.yaml"
# JOBPIPE_DATA can point elsewhere (tests, or keeping data in a backed-up folder)
DATA_DIR = Path(os.environ.get("JOBPIPE_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "pipeline.db"
DASHBOARD_PATH = DATA_DIR / "dashboard.html"

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}\n"
            "Run `python jobpipe.py init` to create your config interactively,\n"
            f"or copy config/{path.stem}.example.yaml to config/{path.name} and edit it."
        )
    with open(path) as f:
        return yaml.safe_load(f)


@functools.lru_cache(maxsize=None)
def profile() -> dict:
    return _load_yaml(PROFILE_PATH)


@functools.lru_cache(maxsize=None)
def companies() -> list[dict]:
    return _load_yaml(CONFIG_DIR / "companies.yaml").get("companies") or []


BLOCKLIST_PATH = CONFIG_DIR / "blocklist.yaml"

_BLOCKLIST_HEADER = """\
# Personal blocklist, managed by `python jobpipe.py skip --block-company / --block-title TERM`.
# Safe to edit by hand. Both lists are hard rejects in scoring:
#   companies:   exact company name, case-insensitive
#   title_terms: word-boundary terms, like title_blockers (trailing * = prefix match)
"""


@functools.lru_cache(maxsize=None)
def blocklist() -> dict:
    """{'companies': [{value, reason}], 'title_terms': [{value, reason}]}; empty if no file."""
    if not BLOCKLIST_PATH.exists():
        return {"companies": [], "title_terms": []}
    with open(BLOCKLIST_PATH) as f:
        data = yaml.safe_load(f) or {}
    norm = lambda e: e if isinstance(e, dict) else {"value": e, "reason": None}
    return {k: [norm(e) for e in (data.get(k) or [])] for k in ("companies", "title_terms")}


def add_block(kind: str, value: str, reason: str | None = None) -> bool:
    """Append one entry to blocklist.yaml. Returns False if already present."""
    data = blocklist()
    if any(e["value"].lower() == value.lower() for e in data[kind]):
        return False
    data[kind].append({"value": value, "reason": reason})
    with open(BLOCKLIST_PATH, "w") as f:
        f.write(_BLOCKLIST_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    blocklist.cache_clear()
    return True


def clear_caches() -> None:
    """Drop cached YAML after the wizard (re)writes config files."""
    profile.cache_clear()
    companies.cache_clear()
    blocklist.cache_clear()


def threshold(key: str, default=None):
    return profile().get("thresholds", {}).get(key, default)
