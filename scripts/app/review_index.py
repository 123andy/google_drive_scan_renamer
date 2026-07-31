"""Maintain PROCESSED/_review/REVIEW_QUEUE.md — a human-readable index of the queue.

Regenerated whenever the queue changes: after a processing run (stage_and_run.py)
and after adjudication (adjudicate.py). Derived purely from the sidecars in _review/,
so it can never disagree with them; it's a convenience view, not a source of truth.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import sidecar

INDEX_NAME = "REVIEW_QUEUE.md"


def _cell(value) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def regenerate(review_dir: Path) -> int:
    """(Re)write REVIEW_QUEUE.md from the sidecars in review_dir. Returns item count.

    No-op (returns 0) if review_dir doesn't exist.
    """
    if not review_dir.exists():
        return 0

    originals = sorted(
        p for p in review_dir.iterdir()
        if p.is_file() and p.suffix.lower() != ".md"
    )

    lines = [
        "# Review queue",
        "",
        f"_{len(originals)} item(s) awaiting review — regenerated "
        f"{datetime.now():%Y-%m-%d %H:%M}._",
        "",
        "Clear with `python3 adjudicate.py` (interactive) or `--list`. "
        "Auto-generated from the sidecars; do not edit by hand.",
        "",
    ]

    if originals:
        lines += [
            "| File | Type | Patient | Doc date | Confidence | Reason |",
            "|------|------|---------|----------|------------|--------|",
        ]
        for p in originals:
            fm, _ = sidecar.read_sidecar(p.with_name(p.name + ".md"))
            lines.append(
                f"| {_cell(p.name)} | {_cell(fm.get('doc_type'))} "
                f"| {_cell(fm.get('patient'))} | {_cell(fm.get('document_date'))} "
                f"| {_cell(fm.get('overall_confidence'))} | {_cell(fm.get('review_reason'))} |"
            )
    else:
        lines.append("_Queue is empty._")
    lines.append("")

    (review_dir / INDEX_NAME).write_text("\n".join(lines), encoding="utf-8")
    return len(originals)
