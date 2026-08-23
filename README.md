# google_drive_scan_renamer

An AI-mediated filing tool for scanned documents. A ScanSnap scanner drops PDFs and images
into a Google Drive folder; this batch OCRs each one, asks an LLM for a descriptive
filename **and** a structured `.md` sidecar, then files both into `PROCESSED/` and drains
the inbox.

> **Status:** largely superseded by the **personal-vault** project, which takes over most of
> this lifting. Kept here as the versioned home of the working ScanSnap pipeline.

## Where things are

- **`scripts/`** — all the code (Dockerized processor + host-side launcher and CLIs).
  Start with [`scripts/README.md`](scripts/README.md) for setup, running, and the review queue.
- **`docs/SIDECAR_SPEC.md`** — the authoritative sidecar contract (frontmatter fields, body
  sections, the correct-don't-fabricate rules).
- **`CLAUDE.md`** — the design/conventions doc for the pipeline and the Drive folder layout.

## Quick start

```bash
cp scripts/.env.example scripts/.env     # add OPENAI_API_KEY
export INBOX_DIR="$HOME/Library/CloudStorage/GoogleDrive-YOUR_GOOGLE_ACCOUNT@gmail.com/My Drive/ScanSnap"
cd scripts && DRY_RUN=true ./run.sh      # analyze + log only; drop DRY_RUN to file for real
```

Docker is the only host requirement — the OCR toolchain (`ocrmypdf`, `tesseract`,
`ghostscript`) ships inside the image. `INBOX_DIR` is mandatory: the code lives in this
checkout rather than inside the Drive folder, and the launcher refuses to run against a
directory that doesn't look like the scan inbox.

## History

The pipeline was developed in place inside the Drive folder
(`My Drive/ScanSnap/scripts/`) and migrated into this repo on 2026-07-30, replacing the
original single-file prototype (`app/main.py`) that only renamed top-level PDFs into
`RENAMED/` via the Drive API. That prototype is preserved at the git tag
**`pre-scansnap-migration`**.
