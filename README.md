# google_drive_scan_renamer
An ai-mediated file renaming tool for scanned pdfs

## Dockerized Python setup with `.env` secrets

This repo includes a Dockerized Python worker that:

- Reads `BASE_DRIVE_URL` and scans top-level `.pdf` files in that folder (no recursion).
- Extracts `scan_date` if filename starts with `yyyy_mm_dd_`.
- Runs OCR with `ocrmypdf`.
- Sends OCR text to OpenAI to generate a filename (<60 chars, lowercase, underscores).
- Prepends `scan_date` when present.
- Renames and moves each file into a destination subfolder in Drive (`DEST_SUBFOLDER`, default `RENAMED`).

### 1) Create your local `.env`

```bash
cp .env.example .env
```

Then edit `.env` and set your real secret values:

```env
BASE_DRIVE_URL=https://drive.google.com/drive/folders/1WTysUIyp01kod80W6MWk-LQPgBz920dp
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
GOOGLE_OAUTH_TOKEN_JSON=
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
DEST_SUBFOLDER=RENAMED
```

Auth behavior:

- Drive auth is OAuth-only via `get_creds()` and `service = build("drive", "v3", ...)`.
- Provide either:
	- `GOOGLE_OAUTH_TOKEN_JSON` (authorized user token JSON), or
	- `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` + `GOOGLE_OAUTH_REFRESH_TOKEN`.

### 1.1) Generate `token.json` with `auth_setup.py` (recommended)

`auth_setup.py` creates a Google OAuth token file (`token.json`) that this app can read via `GOOGLE_OAUTH_TOKEN_JSON`.

1. In Google Cloud Console, create/select a project.
2. Enable the **Google Drive API** for that project.
3. Configure the OAuth consent screen.
4. Create OAuth client credentials (Desktop App).
5. Download the OAuth client file and save it at the repo root as `credentials.json`.

Run the setup script:

```bash
python3 auth_setup.py
```

This opens a browser for consent and writes `token.json` in the repo root.

Convert `token.json` into a single-line JSON string and put it in `.env`:

```bash
python3 -c 'import json; print(json.dumps(json.load(open("token.json")), separators=(",", ":")))'
```

Then set:

```env
GOOGLE_OAUTH_TOKEN_JSON={"token":"...","refresh_token":"...","client_id":"...","client_secret":"...","token_uri":"https://oauth2.googleapis.com/token","scopes":["https://www.googleapis.com/auth/drive"]}
```

When `GOOGLE_OAUTH_TOKEN_JSON` is set, you can leave `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `GOOGLE_OAUTH_REFRESH_TOKEN` empty.

### 2) Build and run with Docker Compose

```bash
docker compose up --build
```

`docker-compose.yml` uses:

```yaml
env_file:
	- .env
```

So variables from `.env` are injected into the container at runtime.

### 3) Alternative: run without Compose

```bash
docker build -t google-drive-scan-renamer .
docker run --rm --env-file .env google-drive-scan-renamer
```

## Security notes

- `.env` is ignored by git and `.dockerignore`.
- Keep only placeholders in `.env.example`.
- Never commit real secrets.

## OAuth scope requirement

When creating your OAuth refresh token, include Drive write scope such as:

- `https://www.googleapis.com/auth/drive`

Without write scope, rename/move operations will fail.
