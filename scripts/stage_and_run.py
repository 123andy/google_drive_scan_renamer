#!/usr/bin/env python3
"""Host-side launcher for the Dockerized ScanSnap processor.

Why this exists: the inbox lives on a Google Drive (File Provider) filesystem, and
bind-mounting that into Docker deadlocks on read (Errno 35). The host reads/writes
Drive natively just fine, so we split responsibilities:

  host (this script)   does all Drive I/O -- stage scans into a local scratch dir,
                       then move the container's results back to PROCESSED and drain
                       the inbox.
  container (app/)     does OCR + LLM + sidecar over a plain local directory.

The container drains ``/work/in`` (moving each original into ``/work/out`` or
``/work/out/_errors``). After it exits, any staged file that is GONE from the scratch
input was handled, so we delete that original from the Drive inbox -- a crash leaves
the original in place for the next run. Stdlib only.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config, review_index  # noqa: E402  (reuse inbox filter + queue index)

# Per-file ledger columns (persistent CSV at scripts/state/ledger.csv).
LEDGER_COLUMNS = [
    "timestamp", "source", "final_name", "location", "doc_type", "needs_review",
    "status", "ocr_chars", "in_tokens", "out_tokens", "cost_usd", "seconds", "error",
]

SCRIPTS_DIR = Path(__file__).resolve().parent
# Scratch must live on a real local disk that Docker Desktop shares (/Users is shared
# by default) -- NOT on the Drive mount.
WORK_DIR = Path(os.getenv("SCANSNAP_WORK", str(Path.home() / ".scansnap_work")))

# Drive paths (host-native). The code now lives in a git checkout rather than inside the
# Drive folder, so the old "inbox is my parent dir" default only holds when this really
# is running from the scan folder -- otherwise INBOX_DIR must be set explicitly. Guard it:
# a wrong inbox means staging (and, after a successful run, DELETING) the wrong files.
INBOX_DIR = Path(os.getenv("INBOX_DIR", str(SCRIPTS_DIR.parent)))
if not os.getenv("INBOX_DIR") and not (INBOX_DIR / "PROCESSED").is_dir():
    raise SystemExit(
        f"Refusing to treat {INBOX_DIR} as the scan inbox (no PROCESSED/ in it).\n"
        "Set INBOX_DIR to the ScanSnap Drive folder, e.g.\n"
        '  INBOX_DIR="$HOME/Library/CloudStorage/GoogleDrive-YOUR_GOOGLE_ACCOUNT@gmail.com/My Drive/ScanSnap" ./run.sh'
    )
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(INBOX_DIR / "PROCESSED")))
STATE_DIR = SCRIPTS_DIR / "state"

DRY_RUN = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "processing.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def unique_path(directory: Path, basename: str, ext: str) -> Path:
    candidate = directory / f"{basename}{ext}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{basename}_{n}{ext}"
        n += 1
    return candidate


def reset_scratch() -> tuple[Path, Path]:
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
    work_in = WORK_DIR / "in"
    work_out = WORK_DIR / "out"
    work_in.mkdir(parents=True, exist_ok=True)
    work_out.mkdir(parents=True, exist_ok=True)
    (WORK_DIR / "state").mkdir(parents=True, exist_ok=True)
    return work_in, work_out


def stage_inbox(work_in: Path) -> list[str]:
    """Copy supported inbox scans into the scratch input dir. Returns staged names.

    SCANSNAP_LIMIT caps how many scans are staged this run (0/unset = all) -- handy
    for a cautious first live run.
    """
    limit = int(os.getenv("SCANSNAP_LIMIT", "0"))
    staged: list[str] = []
    for entry in sorted(INBOX_DIR.iterdir()):
        if entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in config.IGNORED_NAMES:
            continue
        if entry.suffix.lower() not in config.SUPPORTED_EXTS:
            continue
        shutil.copy2(entry, work_in / entry.name)
        staged.append(entry.name)
        if limit and len(staged) >= limit:
            log(f"SCANSNAP_LIMIT={limit} reached; staging stops here")
            break
    return staged


def run_container() -> int:
    env = dict(os.environ, WORK_DIR=str(WORK_DIR))
    # --build keeps the image in sync with app/ changes (fast when layers are cached).
    cmd = ["docker", "compose", "run", "--rm", "--build"]
    if DRY_RUN:
        cmd += ["-e", "DRY_RUN=true"]
    cmd.append("processor")
    log(f"Running container: {' '.join(cmd)}  (WORK_DIR={WORK_DIR})")
    return subprocess.run(cmd, cwd=SCRIPTS_DIR, env=env).returncode


def move_pairs(src_dir: Path, dst_dir: Path, label: str) -> int:
    """Move each original + its ``<name>.md`` sidecar from src to dst, collision-safe.

    Operates only on the top level of src_dir (subdirs like _review/_errors are
    handled by their own call). Returns the count of originals moved.
    """
    if not src_dir.exists():
        return 0
    moved = 0
    for item in sorted(src_dir.iterdir()):
        if item.is_dir() or item.suffix.lower() == ".md":
            continue  # .md travels with its original below
        dst_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(dst_dir, item.stem, item.suffix.lower())
        shutil.move(str(item), str(dest))
        sidecar = item.with_name(item.name + ".md")
        if sidecar.exists():
            shutil.move(str(sidecar), str(dest.with_name(dest.name + ".md")))
        moved += 1
        log(f"Filed -> {label}{dest.name}")
    return moved


def move_tree_to_drive(work_out: Path) -> int:
    """Move container outputs into Drive PROCESSED. Returns count of filed originals.

    Confident docs go to PROCESSED/; low-confidence to PROCESSED/_review/; hard
    failures to PROCESSED/_errors/. Collisions get a ``_n`` suffix on both the file
    and its sidecar.
    """
    if not any(work_out.iterdir()):
        return 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    filed = move_pairs(work_out, OUTPUT_DIR, "PROCESSED/")
    filed += move_pairs(work_out / "_review", OUTPUT_DIR / "_review", "PROCESSED/_review/")

    # Quarantined failures (original + .error.txt, no sidecar pairing).
    err_src = work_out / "_errors"
    if err_src.exists():
        err_dst = OUTPUT_DIR / "_errors"
        err_dst.mkdir(parents=True, exist_ok=True)
        for item in sorted(err_src.iterdir()):
            shutil.move(str(item), str(err_dst / item.name))
            log(f"Quarantined -> PROCESSED/_errors/{item.name}")
    return filed


def relay_ledger(work_state: Path) -> tuple[int, float]:
    """Append the container's per-file ledger (JSONL) to scripts/state/ledger.csv.

    Returns (rows_added, total_cost_usd). The container writes a JSON object per file
    to /work/state/ledger.jsonl; we render those as CSV rows (with a header on first
    write) into the persistent ledger on Drive.
    """
    src = work_state / "ledger.jsonl"
    if not src.exists():
        return 0, 0.0
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        return 0, 0.0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = STATE_DIR / "ledger.csv"
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)
    total_cost = sum(float(r.get("cost_usd") or 0) for r in rows)
    return len(rows), total_cost


def drain_inbox(work_in: Path, staged: list[str]) -> int:
    """Delete Drive originals whose staged copy the container consumed."""
    removed = 0
    for name in staged:
        if not (work_in / name).exists():  # container handled it
            original = INBOX_DIR / name
            if original.exists():
                original.unlink()
                removed += 1
    return removed


def main() -> int:
    log(f"===== stage_and_run started{' [DRY RUN]' if DRY_RUN else ''} =====")
    work_in, work_out = reset_scratch()

    staged = stage_inbox(work_in)
    log(f"Staged {len(staged)} scan(s) from inbox: {INBOX_DIR}")
    if not staged:
        log("Nothing to do.")
        return 0

    rc = run_container()
    log(f"Container exited with code {rc}")

    if DRY_RUN:
        log("[dry-run] leaving inbox and PROCESSED untouched.")
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        return rc

    filed = move_tree_to_drive(work_out)
    removed = drain_inbox(work_in, staged)
    queued = review_index.regenerate(OUTPUT_DIR / "_review")
    ledger_rows, run_cost = relay_ledger(WORK_DIR / "state")
    log(f"Done: filed={filed} inbox_removed={removed} review_queue={queued} "
        f"ledger_rows={ledger_rows} run_cost=${run_cost:.4f}")
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
