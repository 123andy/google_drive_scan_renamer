"""ScanSnap inbox processor -- one-shot, cron-driven batch.

Drains the inbox root: for each new scan it OCRs the file, asks the LLM for a
filename + sidecar, then writes ``<final>.<ext>`` and ``<final>.<ext>.md`` into the
output folder and moves the original out of the root. Per-file errors are isolated
and quarantined so one bad scan never aborts the batch (and never re-bills forever).

Idempotency is folder-state based: a processed scan leaves the root, so the next run
won't see it. See CLAUDE.md and docs/SIDECAR_SPEC.md.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, llm, ocr, sidecar

LOGGER = logging.getLogger("scansnap")


# --- env / logging -------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """Minimal .env loader for non-Docker runs (Docker uses compose env_file)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configure_logging(state_dir: Path) -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    LOGGER.addHandler(stream)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(state_dir / "processing.log")
        file_handler.setFormatter(fmt)
        LOGGER.addHandler(file_handler)
    except Exception as err:  # logging must never abort the run
        stream.handle(LOGGER.makeRecord(
            LOGGER.name, logging.WARNING, __file__, 0,
            "Could not open log file: %s", (err,), None))
    LOGGER.propagate = False


# --- inbox scan ----------------------------------------------------------------
def list_inbox(inbox_dir: Path) -> list[Path]:
    files = []
    for entry in sorted(inbox_dir.iterdir()):
        if entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in config.IGNORED_NAMES:
            continue
        if entry.suffix.lower() not in config.SUPPORTED_EXTS:
            continue
        files.append(entry)
    return files


def unique_path(directory: Path, basename: str, ext: str) -> Path:
    """Avoid clobbering an existing output; append _2, _3, ..."""
    candidate = directory / f"{basename}{ext}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{basename}_{n}{ext}"
        n += 1
    return candidate


# --- per-file processing -------------------------------------------------------
def process_file(path: Path, cfg: config.Config, usage_tracker: dict) -> dict:
    """Process one scan. Returns a metrics dict for the ledger (see main loop)."""
    LOGGER.info("Processing: %s", path.name)
    scan_date, _ = config.extract_scan_date(path.name)
    received_date = config.date_prefix_to_iso(scan_date) or datetime.now().strftime("%Y-%m-%d")

    ocr_text = ocr.extract_text(path, image_dpi=cfg.image_dpi)
    LOGGER.info("OCR characters: %s", len(ocr_text))
    if not ocr_text.strip():
        LOGGER.warning("No text extracted from %s; sidecar will flag review", path.name)

    result, usage = llm.analyze_document(path.name, ocr_text, cfg)
    usage_tracker["requests"] += 1
    usage_tracker["input_tokens"] += usage["input_tokens"]
    usage_tracker["output_tokens"] += usage["output_tokens"]

    final_base = config.build_final_basename(
        scan_date, result.get("filename_descriptive", ""), result.get("tax_year"))
    ext = path.suffix.lower()
    needs_review = bool(result.get("needs_human_review"))
    # Low-confidence docs go to the _review queue; confident ones to output root.
    target_dir = cfg.review_dir if needs_review else cfg.output_dir

    metrics = {
        "doc_type": result.get("doc_type", "unknown"),
        "needs_review": needs_review,
        "ocr_chars": len(ocr_text),
        "in_tokens": usage["input_tokens"],
        "out_tokens": usage["output_tokens"],
    }

    if cfg.dry_run:
        LOGGER.info("[dry-run] would write %s%s (+ .md) to %s", final_base, ext, target_dir)
        metrics.update(final_name=f"{final_base}{ext}", location="(dry-run)", status="dry_run")
        return metrics

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(target_dir, final_base, ext)

    md_text = sidecar.render_sidecar(
        result,
        received_date=received_date,
        generator=cfg.openai_model,
        generated_on=datetime.now().strftime("%Y-%m-%d"),
        ocr_text=ocr_text,
        max_ocr_chars=cfg.max_ocr_chars,
    )

    # Write sidecar first; if it fails we haven't yet moved the original out of root.
    sidecar_path = dest.with_name(dest.name + ".md")
    sidecar_path.write_text(md_text, encoding="utf-8")
    shutil.move(str(path), str(dest))

    where = "_review/" if needs_review else ""
    LOGGER.info("Filed: %s -> %s%s", path.name, where, dest.name)
    metrics.update(
        final_name=dest.name,
        location="_review" if needs_review else "PROCESSED",
        status="review" if needs_review else "filed",
    )
    return metrics


def quarantine(path: Path, cfg: config.Config, error: Exception) -> None:
    """Move a failed scan out of the inbox so it isn't reprocessed/re-billed."""
    if cfg.dry_run:
        return
    try:
        cfg.error_dir.mkdir(parents=True, exist_ok=True)
        dest = unique_path(cfg.error_dir, path.stem, path.suffix.lower())
        shutil.move(str(path), str(dest))
        dest.with_name(dest.name + ".error.txt").write_text(
            f"Failed to process at {datetime.now().isoformat()}\n{type(error).__name__}: {error}\n",
            encoding="utf-8",
        )
        LOGGER.info("Quarantined: %s -> _errors/%s", path.name, dest.name)
    except Exception:
        LOGGER.exception("Could not quarantine %s; leaving it in the inbox", path.name)


def write_ledger(cfg: config.Config, record: dict) -> None:
    """Append one JSON line per file to the ledger (host relays it to a CSV)."""
    try:
        cfg.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with cfg.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        LOGGER.exception("Failed to write ledger record for %s", record.get("source"))


# --- entrypoint ----------------------------------------------------------------
def main() -> None:
    script_dir = Path(__file__).resolve().parent.parent  # scripts/
    load_dotenv(script_dir / ".env")

    cfg = config.Config()
    configure_logging(cfg.state_dir)

    LOGGER.info("===== Run started %s =====", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    cfg.validate()
    LOGGER.info("Inbox: %s  Output: %s  Model: %s%s",
                cfg.inbox_dir, cfg.output_dir, cfg.openai_model,
                "  [DRY RUN]" if cfg.dry_run else "")

    files = list_inbox(cfg.inbox_dir)
    LOGGER.info("Found %s scan(s) to process", len(files))

    usage_tracker = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    processed = failed = 0
    total_cost = total_seconds = 0.0
    for path in files:
        # One ledger row per file: timing measured here so it covers OCR + LLM + move,
        # and the error path is logged uniformly with the success path.
        t0 = time.monotonic()
        record = {"timestamp": datetime.now().isoformat(timespec="seconds"),
                  "source": path.name}
        try:
            record.update(process_file(path, cfg, usage_tracker))
            processed += 1
        except Exception as error:  # per-file isolation
            failed += 1
            LOGGER.exception("FAILED %s: %s", path.name, error)
            quarantine(path, cfg, error)
            record.update(status="error", location="_errors",
                          error=f"{type(error).__name__}: {error}")

        record["seconds"] = round(time.monotonic() - t0, 2)
        record["cost_usd"] = round(
            cfg.cost_usd(record.get("in_tokens", 0), record.get("out_tokens", 0)), 6)
        write_ledger(cfg, record)
        total_cost += record["cost_usd"]
        total_seconds += record["seconds"]

    LOGGER.info(
        "Run complete: processed=%s failed=%s requests=%s input_tokens=%s "
        "output_tokens=%s cost=$%.4f seconds=%.1f",
        processed, failed, usage_tracker["requests"], usage_tracker["input_tokens"],
        usage_tracker["output_tokens"], total_cost, total_seconds,
    )


if __name__ == "__main__":
    main()
