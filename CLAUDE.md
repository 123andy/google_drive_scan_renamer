# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A cron-driven **document-scanning pipeline**. Scans (PDFs and images) drop into the root of
a Google Drive folder from a ScanSnap scanner. Each run:

1. Picks up new files sitting in the inbox (the Drive folder's root).
2. Extracts text (OCR), then asks an LLM to produce **two** things per document:
   - a good, descriptive **filename**, and
   - a sidecar **`.md`** file written next to the renamed original (the structured
     summary + cleaned transcript defined in `docs/SIDECAR_SPEC.md`).
3. Moves the renamed file **and** its sidecar **out of the inbox** so the next run doesn't
   reprocess them.

**Status: mostly retired.** The **personal-vault** project takes over most of this work.
Treat this repo as maintenance-only — keep it running, don't invest in new features here.

## Where the code runs from (important)

The pipeline was developed *inside* the Drive folder (`My Drive/ScanSnap/scripts/`) and was
migrated into this repo on 2026-07-30. Consequences to know before running or changing anything:

- **`INBOX_DIR` is now mandatory.** `stage_and_run.py` used to default the inbox to its own
  parent directory, which was the scan folder. From a checkout that default is wrong and
  dangerous (a successful run **deletes** staged originals from the inbox), so the launcher
  refuses to start unless `INBOX_DIR` is set or its parent contains a `PROCESSED/`.
  `adjudicate.py` / `backfill_transcripts.py` want `OUTPUT_DIR="$INBOX_DIR/PROCESSED"` for
  the same reason.
- **`scripts/state/`** (`ledger.csv`, `processing.log`) is runtime state — gitignored, and the
  historical ledger still lives with the old copy in Drive. It was not migrated.
- The old copy under `My Drive/ScanSnap/scripts/` is not git-tracked. **This repo is the
  source of truth**; don't edit the Drive copy.

## Layout

- **`scripts/`** — all code, config, and (untracked) state. `scripts/README.md` covers setup,
  running, the ledger, and the review-queue CLI.
- **`docs/`** — `SIDECAR_SPEC.md`, the authoritative sidecar contract.
- Root holds only this file, `README.md`, `LICENSE`, and the uv dev-env files.

## Pipeline

`stage inbox → extract text` (skip OCR when the PDF already has a text layer, else run
`ocrmypdf`; extract with `pypdf`) → **one** LLM call producing the filename **and** the
sidecar per `docs/SIDECAR_SPEC.md` → write `<final_name>.<ext>` and `<final_name>.<ext>.md`
→ move both out of the inbox into `PROCESSED/` → append to the ledger. Per-file errors are
isolated so one bad scan never aborts the batch.

Because the Drive mount can't be bind-mounted into Docker (reading its File Provider virtual
files through Docker's file sharing deadlocks, Errno 35), the work is split: the **host**
(`stage_and_run.py`) does all Drive I/O into a local scratch dir, and the **container**
(`app/`) does OCR + LLM + sidecar over a plain local directory.

## The sidecar contract

Full spec: **`docs/SIDECAR_SPEC.md`** (authoritative — keep it in sync with the code).
Summary of what the LLM must produce per document:

- **YAML frontmatter** — the machine-readable contract: `doc_type` (from a controlled
  vocabulary), `patient`, `other_parties`, `document_date`, `received_date`,
  `identifiers[]` (each as `raw` + `canonical` + `confidence`), `amounts`, `dates`,
  `provider`, `overall_confidence`, `needs_human_review` (+ `review_reason`).
- **Markdown body** — one-line title, a 2–4 sentence factual **Summary**, **Key facts**,
  **OCR notes**, and an optional **Cleaned transcript** (makes the scan grep-able).
- **Correct, but never fabricate.** Treat OCR as untrusted: fix obvious confusable-char
  slips (`O↔0`, `I↔1`, `S↔5`, …) only when the result fits a known format; keep the
  verbatim OCR in `identifiers[].raw`; never invent a value to fill a field; surface
  ambiguity in **OCR notes** and via `needs_human_review`.
- **Generated once.** A scan is immutable, so its sidecar is produced a single time and
  is safe to cache forever — no regeneration, no staleness.
- **Identifiers vs. person-level numbers.** Only document-specific near-unique IDs go in
  `identifiers[]` (used for content-dedup against already-filed originals); person-level
  numbers like a member/group ID go under Key facts, not `identifiers[]`.

## Filename convention

e.g. `2026_04_29__2026_Welch_Pasteur_Allergy_Statement_Andrew_Martin_x2467.pdf`:

- Prefix `YYYY_MM_DD__` — the scan date, taken from the original scan filename when it
  carries one; double underscore separates the date from the descriptive part.
- Descriptive part encodes (when present): the **year**, **tax form** name (1040, K-1,
  W-2…), **institution**, key parties, and **account numbers truncated to `x<last4>`**.
- **Tax documents** also get an **`FY<tax_year>_`** prefix on the descriptive part (e.g.
  `2026_06_10__FY2026_IRS_EstTaxPayment_JPM_x3180`). The tax year is the year the document
  *applies to* — often not its date — and is captured in the sidecar `tax_year` field.
- Sanitize to `[A-Za-z0-9_]` only.

## Drive folder map (the data side)

- **root** — the scan inbox; the job reads only here. Also holds that folder's own
  `CLAUDE.md` and the run log.
- **`PROCESSED/`** — renamed files + their sidecars. **`PROCESSED/_review/`** is the
  human-review queue for docs flagged `needs_human_review` (cleared by `scripts/adjudicate.py`,
  which stamps the sidecar and moves the pair up), and **`PROCESSED/_errors/`** quarantines
  scans that failed (with a `.error.txt`) so the inbox still drains. `RENAMED/` holds the old
  prototype's flat output history.
- **Out of scope for the automated job** (manual filing areas — never move files here
  automatically): `ORGANIZATION AREA/<category>/`, `DONE/`, `Receipts/`.

## Conventions & gotchas

- **Idempotency** is folder-state based: never leave a processed scan in the inbox.
- **Per-file isolation**: catch and log per-document failures; keep going.
- **OCR is untrusted input** — the correct-don't-fabricate rules in the spec are the
  product logic; change them deliberately.
- The LLM call uses **OpenAI structured outputs** (`json_schema`), so `OPENAI_MODEL` must be
  a model that supports them.
- **Secrets** (`scripts/.env`, API keys) are gitignored and never committed or echoed.
