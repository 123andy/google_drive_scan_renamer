#!/usr/bin/env python3
"""Adjudicate the human-review queue (PROCESSED/_review/).

Walks each low-confidence document the processor flagged, shows why it was flagged
plus the key extracted fields, opens the scan so you can eyeball it, and lets you:

  [a]pprove  stamp the sidecar (needs_human_review: false, reviewed_by/date) and
             move the pair up to PROCESSED/
  [r]ename   give it a better filename, then approve+move
  [e]dit     open the sidecar .md in $EDITOR to fix fields by hand, then re-prompt
  [o]pen     re-open the scan
  [s]kip     leave it in _review for later
  [q]uit

The sidecar frontmatter stays the single source of truth; approving is what keeps
folder location and the needs_human_review flag in agreement. Host-side, runs on the
Drive filesystem directly (no Docker). Stdlib + PyYAML.

Usage:
  python3 adjudicate.py            # interactive
  python3 adjudicate.py --list     # just print the queue and exit
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import review_index  # noqa: E402
from app.sidecar import read_sidecar, write_sidecar  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(SCRIPTS_DIR.parent / "PROCESSED")))
REVIEW_DIR = OUTPUT_DIR / "_review"


# --- queue discovery -----------------------------------------------------------
def queue_items() -> list[Path]:
    """Originals awaiting review (the non-.md files in _review/)."""
    if not REVIEW_DIR.exists():
        return []
    return sorted(p for p in REVIEW_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() != ".md")


def unique_path(directory: Path, basename: str, ext: str) -> Path:
    candidate = directory / f"{basename}{ext}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{basename}_{n}{ext}"
        n += 1
    return candidate


# --- display -------------------------------------------------------------------
def show(original: Path, fm: dict, n: int, total: int) -> None:
    print("\n" + "=" * 70)
    print(f"[{n}/{total}] {original.name}")
    print("-" * 70)
    reason = fm.get("review_reason") or "(no reason recorded)"
    print(f"  reason:        {reason}")
    print(f"  doc_type:      {fm.get('doc_type', '?')}")
    print(f"  patient:       {fm.get('patient', '?')}")
    print(f"  document_date: {fm.get('document_date', '?')}")
    print(f"  provider:      {fm.get('provider', '?')}")
    print(f"  confidence:    {fm.get('overall_confidence', '?')}")
    ids = fm.get("identifiers") or []
    if ids:
        print("  identifiers:")
        for i in ids:
            can = i.get("canonical")
            arrow = f" -> {can}" if can and can != i.get("raw") else ""
            print(f"    - {i.get('kind')}: {i.get('raw')}{arrow} ({i.get('confidence')})")
    print("=" * 70)


NO_OPEN = False  # set by --no-open; skip launching the scan viewer


def open_scan(path: Path) -> None:
    if NO_OPEN:
        return
    try:
        subprocess.run(["open", str(path)], check=False)
    except Exception as err:
        print(f"  (could not open: {err})")


# --- actions -------------------------------------------------------------------
def approve(original: Path, sidecar: Path, fm: dict, body: str, new_base: str | None) -> None:
    """Stamp the sidecar as reviewed and move the pair to PROCESSED/."""
    fm["needs_human_review"] = False
    fm["reviewed_by"] = os.getenv("USER", "manual")
    fm["reviewed_date"] = date.today().isoformat()

    base = new_base or original.stem
    ext = original.suffix.lower()
    dest = unique_path(OUTPUT_DIR, base, ext)

    if sidecar.exists():
        write_sidecar(sidecar, fm, body)
    original.rename(dest)
    if sidecar.exists():
        sidecar.rename(dest.with_name(dest.name + ".md"))
    print(f"  approved -> PROCESSED/{dest.name}")


def resolve_editor() -> str:
    """$EDITOR if set; else TextMate (`mate -w`) when available; else vi.

    `mate -w` waits until the tab closes so we can reload the user's edits; a bare
    `mate` returns immediately. $EDITOR pointed at a GUI editor should include its own
    wait flag (e.g. "code -w").
    """
    editor = os.getenv("EDITOR")
    if editor:
        return editor
    if shutil.which("mate"):
        return "mate -w"
    return "vi"


def edit_in_editor(sidecar: Path) -> None:
    subprocess.run(shlex.split(resolve_editor()) + [str(sidecar)], check=False)


# --- main loop -----------------------------------------------------------------
def main(argv: list[str]) -> int:
    global NO_OPEN
    NO_OPEN = "--no-open" in argv
    items = queue_items()
    if not items:
        print(f"Review queue is empty ({REVIEW_DIR}).")
        return 0

    if "--list" in argv:
        print(f"{len(items)} item(s) in {REVIEW_DIR}:")
        for p in items:
            fm, _ = read_sidecar(p.with_name(p.name + ".md"))
            print(f"  - {p.name}  [{fm.get('doc_type', '?')}]  {fm.get('review_reason', '')}")
        return 0

    total = len(items)
    approved = skipped = 0
    for n, original in enumerate(items, start=1):
        sidecar = original.with_name(original.name + ".md")
        fm, body = read_sidecar(sidecar)
        show(original, fm, n, total)
        open_scan(original)

        while True:
            choice = input("  [a]pprove [r]ename [e]dit [o]pen [s]kip [q]uit > ").strip().lower()
            if choice in ("a", "approve"):
                approve(original, sidecar, fm, body, None)
                approved += 1
                break
            if choice in ("r", "rename"):
                suggested = original.stem
                new_base = input(f"  new filename (no extension) [{suggested}]: ").strip() or suggested
                approve(original, sidecar, fm, body, new_base)
                approved += 1
                break
            if choice in ("e", "edit"):
                edit_in_editor(sidecar)
                fm, body = read_sidecar(sidecar)  # reload after manual edit
                show(original, fm, n, total)
                continue
            if choice in ("o", "open"):
                open_scan(original)
                continue
            if choice in ("s", "skip", ""):
                skipped += 1
                break
            if choice in ("q", "quit"):
                review_index.regenerate(REVIEW_DIR)
                print(f"\nStopped. approved={approved} skipped={skipped} "
                      f"remaining={total - approved - skipped}")
                return 0
            print("  (unrecognized -- use a/r/e/o/s/q)")

    review_index.regenerate(REVIEW_DIR)
    print(f"\nDone. approved={approved} skipped={skipped} of {total}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
