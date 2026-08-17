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


def clear_caches() -> None:
    """Drop cached YAML after the wizard (re)writes config files."""
    profile.cache_clear()
    companies.cache_clear()


def threshold(key: str, default=None):
    return profile().get("thresholds", {}).get(key, default)
