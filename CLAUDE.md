# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A batch worker that scans the top-level `.pdf` files of one Google Drive folder, OCRs each one, asks an OpenAI model for a descriptive filename, then renames and moves the file into a destination subfolder (`RENAMED` by default). It is a one-shot job: `main()` runs once and exits — there is no loop, queue, or scheduler. Re-running is the way to process newly added files.

## Running

The worker depends on the `ocrmypdf` system binary (plus tesseract/ghostscript/qpdf), which the Dockerfile installs. Local runs without these will fail at `check_dependencies()`. Prefer Docker:

```bash
docker compose up --build              # reads .env via env_file
# or
docker build -t google-drive-scan-renamer .
docker run --rm --env-file .env google-drive-scan-renamer
```

Run the parse-logic self-tests (no Drive/OpenAI calls, exits early):

```bash
RUN_SELF_TESTS=1 python app/main.py    # exercises run_self_tests() / extract_scan_date
```

There is no test framework, linter, or CI configured. `run_self_tests()` in `app/main.py` is the only test harness — extend it there when adding parsing logic.

## Auth setup

Drive access is OAuth-only. Generate a token locally with `python3 auth_setup.py` (needs `credentials.json`, a Desktop-App OAuth client, at repo root). It writes `token.json` and offers to inline it into `.env` as `GOOGLE_OAUTH_TOKEN_JSON`. Use `--force-reauth` to discard an existing token. The OAuth client must request the `https://www.googleapis.com/auth/drive` scope (write access) — read-only scopes make rename/move fail.

At runtime, `get_creds()` accepts **either** `GOOGLE_OAUTH_TOKEN_JSON` **or** the triplet `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` + `GOOGLE_OAUTH_REFRESH_TOKEN`. See README.md for the full setup walkthrough.

## Required environment

Required: `BASE_DRIVE_URL`, `OPENAI_API_KEY`, and one of the two auth options above. Optional: `OPENAI_MODEL` (default `gpt-4.1-mini`), `DEST_SUBFOLDER` (default `RENAMED`), `OPENAI_INPUT_COST_PER_1M` / `OPENAI_OUTPUT_COST_PER_1M` (for cost logging only; default 0.0 → reported cost is $0). `BASE_DRIVE_URL` is a full Drive folder URL — `parse_folder_id()` extracts the ID from the `/folders/<id>` path or an `?id=` query param.

## Processing pipeline (app/main.py)

`main()` → `read_env()` → `build_drive_service()` → `list_top_level_pdfs()` → per-file `process_pdf_file()`. Key behaviors to know before changing anything:

- **Scope is strictly top-level, no recursion.** `list_top_level_pdfs()` queries `'<folder>' in parents`. Files inside subfolders (including `RENAMED`) are never touched.
- **Per-file re-validation guards against races.** `process_pdf_file()` re-checks `is_direct_child_of_folder()` both before OCR and again before rename, skipping files that moved out of the source folder mid-run. Preserve these checks when refactoring.
- **OCR is conditional and has a fallback chain.** `run_ocr()` skips OCR entirely if `pdf_has_extractable_text()` finds ≥20 chars (just copies the file), otherwise tries two `ocrmypdf` invocations (with then without deskew/optimize) and finally falls back to the original PDF. OCR uses `--skip-text` so existing text layers are never re-OCR'd.
- **Only the first 1000 chars of OCR text go to the LLM.** See `generate_filename_with_llm()`. The rename prompt encodes domain rules for tax/financial documents (tax form names, years, account-number truncation to `x<last4>`, trust abbreviations) — that prompt is the product logic, edit it deliberately.
- **OpenAI call uses the Responses API** (`client.responses.create`, `completion.output_text`), not chat completions. `temperature` is omitted for `gpt-5*` models. `extract_usage_tokens()` reads both `input/output_tokens` and `prompt/completion_tokens` field names to stay compatible across API shapes.
- **Filenames** are sanitized to `[A-Za-z0-9_]`, capped at 60 chars (`sanitize_filename`). If the source filename starts with `yyyy_mm_dd_` (or is just `yyyy_mm_dd`), that date is extracted (`extract_scan_date`) and prepended as `<date>__<name>`. Date validity is not checked — `2026_99_99` is accepted.
- **Logging is dual-sink.** All logs go to stdout and an in-memory `RUN_LOG_BUFFER`. At the end of a run, `append_logs_to_drive_file()` appends the buffer to a `google_drive_scan_renamer.log` text file inside the source Drive folder. Per-file exceptions are caught and logged so one bad file doesn't abort the batch.

## Secrets

`credentials.json`, `token.json`, `.env`, and `gws_credentials.json` hold live secrets and are gitignored — do not commit them or echo their contents.
