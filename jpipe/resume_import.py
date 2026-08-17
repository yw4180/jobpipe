"""`jobpipe import-resume <resume.pdf>` — draft profile fields from a resume.

Extracts the PDF text with pypdf, asks the configured LLM backend
(see jpipe/enrich.py) for structured career facts, prints the draft, and on
confirmation merges it into config/profile.yaml. Merging is additive: it
never deletes anything you already configured.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import PROFILE_PATH, clear_caches, profile

MAX_RESUME_CHARS = 15_000

RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "current_title": {
            "type": ["string", "null"],
            "description": "e.g. 'Data Engineer @ Acme Corp'",
        },
        "years_experience": {
            "type": ["integer", "null"],
            "description": "Full-time professional years; internships excluded.",
        },
        "target_titles": {
            "type": "array", "items": {"type": "string"},
            "description": "3-6 lowercase job titles this resume is competitive for.",
        },
        "keywords": {
            "type": "array", "items": {"type": "string"},
            "description": "Up to 25 lowercase tools/technologies actually used in the resume.",
        },
        "locations": {
            "type": "array", "items": {"type": "string"},
            "description": "Cities the candidate is based in or lists.",
        },
    },
    "required": ["name", "current_title", "years_experience",
                 "target_titles", "keywords", "locations"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a resume parser. Extract structured career facts from the resume "
    "text. Only use what is stated; use null or an empty list when unsure."
)


def _pdf_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("import-resume needs pypdf:  pip install pypdf")
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"No such file: {p}")
    try:
        reader = PdfReader(str(p))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        raise SystemExit(f"Could not read {p.name}: {type(e).__name__}: {e}")
    text = text.strip()
    if not text:
        raise SystemExit(
            f"{p.name} has no extractable text (scanned/image PDF?). "
            "Export a text-based PDF and try again, or run `python jobpipe.py init`."
        )
    return text[:MAX_RESUME_CHARS]


def _merge(p: dict, draft: dict) -> list[str]:
    """Merge the draft into the profile dict. Returns human-readable change notes."""
    notes: list[str] = []
    cand = p.setdefault("candidate", {})
    if draft.get("name"):
        cand["name"] = draft["name"]
        notes.append(f"candidate.name = {draft['name']}")
    if draft.get("current_title"):
        cand["current"] = draft["current_title"]
        notes.append(f"candidate.current = {draft['current_title']}")
    if draft.get("years_experience") is not None:
        cand["years_experience"] = draft["years_experience"]
        notes.append(f"candidate.years_experience = {draft['years_experience']}")

    titles = p.setdefault("titles", {})
    strong = titles.setdefault("strong", [])
    added = [t for t in (draft.get("target_titles") or [])
             if t and t.lower() not in {s.lower() for s in strong}]
    strong.extend(t.lower() for t in added)
    if added:
        notes.append(f"titles.strong += {added}")

    kws = p.setdefault("keywords", {})
    new_kws = [k for k in (draft.get("keywords") or []) if k and k.lower() not in kws]
    for k in new_kws:
        kws[k.lower()] = 4
    if new_kws:
        notes.append(f"keywords += {new_kws} (weight 4)")
    return notes


def run(path: str, yes: bool = False) -> None:
    if not PROFILE_PATH.exists():
        raise SystemExit(
            "No config/profile.yaml yet — run `python jobpipe.py init` first, "
            "then re-run import-resume to refine it."
        )

    text = _pdf_text(path)
    print(f"Extracted {len(text)} characters. Asking the LLM to parse the resume…")
    from . import enrich  # lazy: pulls in the anthropic SDK / claude CLI check

    draft = enrich.llm_complete(SYSTEM, "Resume text:\n\n" + text, RESUME_SCHEMA)

    print("\nDraft extracted from the resume:\n")
    print(yaml.safe_dump(draft, sort_keys=False, allow_unicode=True))
    if draft.get("locations"):
        print("Note: locations are only a suggestion — edit the structured "
              "locations.tiers section of config/profile.yaml by hand if needed.\n")

    if not yes:
        try:
            ok = input("Apply to config/profile.yaml? [y/N]: ").strip().lower()
        except EOFError:
            ok = ""
        if not ok.startswith("y"):
            print("Nothing written.")
            return

    p = profile()
    notes = _merge(p, draft)
    if not notes:
        print("Nothing new to merge — profile already covers everything extracted.")
        return
    with open(PROFILE_PATH, "w") as f:
        f.write("# Updated by `python jobpipe.py import-resume` — see "
                "config/profile.example.yaml for field docs.\n\n")
        yaml.safe_dump(p, f, sort_keys=False, allow_unicode=True, width=100)
    clear_caches()
    print("Merged into config/profile.yaml:")
    for n in notes:
        print(f"  • {n}")
    print("\nRun `python jobpipe.py rescore` to apply the new profile to stored jobs.")
