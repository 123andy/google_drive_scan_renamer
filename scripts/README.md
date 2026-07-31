# ScanSnap processor

One-shot, Docker-packaged batch that drains the scan inbox (the ScanSnap Drive folder
root), OCRs each new PDF/image, asks an LLM for a descriptive filename **and** a sidecar
`.md` (per `../docs/SIDECAR_SPEC.md`), then writes both into `PROCESSED/` and moves the
original out of the root. See `../CLAUDE.md` for the full design.

The OCR toolchain (`ocrmypdf`, `tesseract`, `ghostscript`) ships inside the image, so
the host needs only Docker.

## Setup

```bash
cd scripts
cp .env.example .env      # then put your OPENAI_API_KEY in it
```

## Run

The code lives in this git checkout, **not** inside the Drive folder, so the host-side
launcher needs to be told where the inbox is — `INBOX_DIR`. Without it `run.sh` refuses
to start (staging the wrong directory would delete the wrong originals).

```bash
export INBOX_DIR="$HOME/Library/CloudStorage/GoogleDrive-123andy@gmail.com/My Drive/ScanSnap"
cd scripts
./run.sh                  # process the inbox
DRY_RUN=true ./run.sh     # OCR + analyze + log, but move/write nothing
```

`adjudicate.py` and `backfill_transcripts.py` work off `PROCESSED/` and need the matching
`OUTPUT_DIR="$INBOX_DIR/PROCESSED"` for the same reason.

or directly:

```bash
docker compose run --rm processor
```

## What it does per file

1. Picks up supported files (`.pdf .jpg .jpeg .png .tif .tiff`) sitting in the root.
2. Extracts text — text-layer PDFs via `pypdf`; everything else via `ocrmypdf`.
3. One LLM call returns the filename parts + the full sidecar contract.
4. Writes the renamed file + `…​.<ext>.md` sidecar and moves the original. Confident
   docs go to `PROCESSED/<YYYY_MM_DD>__<desc>.<ext>`; low-confidence ones
   (`needs_human_review: true`) go to `PROCESSED/_review/` instead.
5. Hard failures are quarantined to `PROCESSED/_errors/` (with a `.error.txt`) so the
   inbox still drains and the cron doesn't re-bill a poison file. Per-file errors never
   abort the batch.

Logs (`scripts/state/`):
- `processing.log` — freeform per-run log (what was filed, run summary with totals).
- `ledger.csv` — **one row per processed file**: timestamp, source, final name, location
  (`PROCESSED`/`_review`/`_errors`), doc_type, status, OCR chars, input/output tokens,
  **`cost_usd`**, and **`seconds`** (wall time covering OCR + LLM + move). Appended every
  run; open it in Numbers/Excel or `column -t -s, ledger.csv`. Cost uses the per-1M rates
  in `.env` (`OPENAI_INPUT_COST_PER_1M` / `OPENAI_OUTPUT_COST_PER_1M`). Dry runs are not
  recorded.

## Review queue

Low-confidence documents land in `PROCESSED/_review/` (still with their sidecar). An
index, `PROCESSED/_review/REVIEW_QUEUE.md`, is auto-regenerated from the sidecars after
every processing run and every adjudication, so you can glance at the queue (file, type,
patient, reason) straight from Drive without the CLI. It's derived output — don't edit it.

Clear the queue with the adjudication CLI:

```bash
cd scripts
python3 adjudicate.py --list     # show the queue + why each was flagged
python3 adjudicate.py            # walk each: opens the scan, [a]pprove/[r]ename/[e]dit/[s]kip
```

Approving stamps the sidecar (`needs_human_review: false`, `reviewed_by`,
`reviewed_date`) and moves the pair up to `PROCESSED/`. The sidecar frontmatter stays
the single source of truth, so location and the flag never disagree. Flags: `--list`
(non-interactive), `--no-open` (don't launch the scan viewer). `[e]dit` opens the
sidecar in `$EDITOR` if set, else TextMate (`mate -w`) when available, else `vi`. Point
`$EDITOR` at a GUI editor only with its wait flag (e.g. `code -w`).

## Layout

```
scripts/
  app/
    main.py      orchestrator (scan inbox, isolate per-file errors, move out)
    ocr.py       text-layer detection + ocrmypdf
    llm.py       OpenAI structured-output call (filename + sidecar fields)
    sidecar.py   renders YAML frontmatter + markdown body
    config.py    doc_type vocabulary, identifier kinds, filename/date helpers
  stage_and_run.py  host launcher (Drive I/O + Docker); run via ./run.sh
  adjudicate.py     review-queue CLI for PROCESSED/_review/
  backfill_transcripts.py  regenerate sidecars in place (no PDF moves) — e.g. to
                           refresh full transcripts after a logic change
  Dockerfile  docker-compose.yml  requirements.txt  .env(.example)  run.sh
```

## Cron (example)

Run every 15 minutes via the host's crontab:

```cron
*/15 * * * * cd "/Users/andy/Projects/personal/google_drive_scan_renamer/scripts" && INBOX_DIR="/Users/andy/Library/CloudStorage/GoogleDrive-123andy@gmail.com/My Drive/ScanSnap" ./run.sh >> state/cron.log 2>&1
```
