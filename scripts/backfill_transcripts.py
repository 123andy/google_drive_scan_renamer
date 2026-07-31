#!/usr/bin/env python3
"""Host launcher: backfill full transcripts into already-filed sidecars, IN PLACE.

Safe by design after the Drive data-loss incident: the only Drive write is
overwriting each ``<name>.md`` sidecar at its existing path. PDFs are copied (a read)
into a local scratch dir for OCR -- never moved, renamed, or deleted.

Targets default to documents whose OCR exceeded the old 16k cap (from the ledger,
filtered to files that still exist in PROCESSED). Pass explicit filenames as args to
override.

    python3 backfill_transcripts.py
    python3 backfill_transcripts.py "2026_06_17__...COBRA....pdf" "...other.pdf"
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from app import sidecar  # noqa: E402  (read_sidecar for the review-flag check)

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(SCRIPTS_DIR.parent / "PROCESSED")))
LEDGER_CSV = SCRIPTS_DIR / "state" / "ledger.csv"
WORK_DIR = Path(os.getenv("SCANSNAP_BACKFILL_WORK", str(Path.home() / ".scansnap_backfill")))
OLD_CAP = 16000  # docs above this had truncated transcripts under the old default


def derive_targets() -> list[str]:
    """Final names with OCR >= OLD_CAP (latest per name) that still exist in PROCESSED."""
    if not LEDGER_CSV.exists():
        return []
    longest: dict[str, int] = {}
    for row in csv.DictReader(LEDGER_CSV.open()):
        name, chars = row.get("final_name", ""), row.get("ocr_chars", "")
        if name and chars.isdigit():
            longest[name] = max(longest.get(name, 0), int(chars))
    return sorted(n for n, c in longest.items()
                  if c >= OLD_CAP and (OUTPUT_DIR / n).exists())


def main(argv: list[str]) -> int:
    targets = argv or derive_targets()
    if not targets:
        print("No backfill targets found.")
        return 0

    # Stage read-only copies into scratch.
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    work_in, work_out = WORK_DIR / "in", WORK_DIR / "out"
    work_in.mkdir(parents=True); work_out.mkdir(parents=True); (WORK_DIR / "state").mkdir()

    staged = []
    for name in targets:
        src = OUTPUT_DIR / name
        if not src.exists():
            print(f"  MISSING, skip: {name}")
            continue
        shutil.copy2(src, work_in / name)
        staged.append(name)
    print(f"Staged {len(staged)} doc(s) for backfill.")
    if not staged:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        return 0

    # Run the container in backfill mode (produces <name>.md in /work/out, no renames).
    env = dict(os.environ, WORK_DIR=str(WORK_DIR))
    rc = subprocess.run(
        ["docker", "compose", "run", "--rm", "--build", "processor", "python", "-m", "app.backfill"],
        cwd=SCRIPTS_DIR, env=env).returncode
    if rc != 0:
        print(f"Container exited {rc}; not touching sidecars.")
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        return rc

    # Overwrite each sidecar in place. No PDF is moved/renamed/deleted.
    updated, flagged = 0, []
    for name in staged:
        new_md = work_out / (name + ".md")
        if not new_md.exists():
            print(f"  no sidecar produced, left unchanged: {name}")
            continue
        shutil.copyfile(new_md, OUTPUT_DIR / (name + ".md"))  # in-place content overwrite
        updated += 1
        fm, _ = sidecar.read_sidecar(OUTPUT_DIR / (name + ".md"))
        if fm.get("needs_human_review"):
            flagged.append(name)
        print(f"  updated sidecar: {name}")

    print(f"\nBackfilled {updated}/{len(staged)} sidecars (in place).")
    if flagged:
        print("NOTE: the refreshed analysis now flags these for review (left in place, "
              "not moved):")
        for n in flagged:
            print(f"  - {n}")
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
