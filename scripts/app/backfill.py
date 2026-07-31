"""Backfill mode: regenerate sidecars in place, WITHOUT renaming or moving the PDF.

Reads PDFs/images from INBOX_DIR and writes ``<original_name>.md`` to OUTPUT_DIR,
keyed by the input's existing filename (no rename). The host launcher
(backfill_transcripts.py) then overwrites each existing sidecar in PROCESSED at its
current path -- no file moves/renames/deletes, which keeps Drive sync churn (and its
data-loss risk) to a minimum.

Used to refresh sidecars after a logic change (e.g. the 32k cap + full-transcript
preservation) for documents that were already filed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from . import config, llm, ocr, sidecar

LOGGER = logging.getLogger("scansnap")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    cfg = config.Config()
    cfg.validate()

    in_dir, out_dir = cfg.inbox_dir, cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    files = [p for p in sorted(in_dir.iterdir())
             if not p.is_dir() and not p.name.startswith(".")
             and p.suffix.lower() in config.SUPPORTED_EXTS]
    LOGGER.info("Backfill: %s file(s)", len(files))

    for path in files:
        try:
            scan_date, _ = config.extract_scan_date(path.name)
            received = config.date_prefix_to_iso(scan_date) or today

            ocr_text = ocr.extract_text(path, image_dpi=cfg.image_dpi)
            result, _ = llm.analyze_document(path.name, ocr_text, cfg)

            md = sidecar.render_sidecar(
                result, received_date=received, generator=cfg.openai_model,
                generated_on=today, ocr_text=ocr_text, max_ocr_chars=cfg.max_ocr_chars)
            (out_dir / (path.name + ".md")).write_text(md, encoding="utf-8")

            tail = max(0, len(ocr_text) - cfg.max_ocr_chars)
            LOGGER.info("Regenerated %s.md  (ocr_chars=%s, raw_tail=%s, review=%s)",
                        path.name, len(ocr_text), tail, result.get("needs_human_review"))
        except Exception as error:  # isolate per file
            LOGGER.exception("Backfill FAILED for %s: %s", path.name, error)


if __name__ == "__main__":
    main()
