import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import io
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from openai import OpenAI
from pypdf import PdfReader

DATE_PREFIX_PATTERN = re.compile(r"^(\d{4}_\d{2}_\d{2})_(.+)$")
DATE_ONLY_PATTERN = re.compile(r"^(\d{4}_\d{2}_\d{2})$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9_]+")

SCOPES = ["https://www.googleapis.com/auth/drive"]
LOG_FILE_NAME = "google_drive_scan_renamer.log"
LOGGER = logging.getLogger("google_drive_scan_renamer")
RUN_LOG_BUFFER = io.StringIO()


def configure_logging() -> None:
    if LOGGER.handlers:
        return

    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    memory_handler = logging.StreamHandler(RUN_LOG_BUFFER)
    memory_handler.setFormatter(formatter)

    LOGGER.addHandler(stream_handler)
    LOGGER.addHandler(memory_handler)
    LOGGER.propagate = False


def get_run_log_text() -> str:
    return RUN_LOG_BUFFER.getvalue()


def parse_float_env(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise RuntimeError(f"Invalid float value for {name}: {raw}") from error

def get_creds(env: dict) -> Credentials:
    creds: Optional[Credentials] = None

    token_json = env.get("GOOGLE_OAUTH_TOKEN_JSON")
    if token_json:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    elif all(
        [
            env.get("GOOGLE_OAUTH_CLIENT_ID"),
            env.get("GOOGLE_OAUTH_CLIENT_SECRET"),
            env.get("GOOGLE_OAUTH_REFRESH_TOKEN"),
        ]
    ):
        creds = Credentials(
            token=None,
            refresh_token=env["GOOGLE_OAUTH_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=env["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=env["GOOGLE_OAUTH_CLIENT_SECRET"],
            scopes=SCOPES,
        )
    else:
        raise RuntimeError(
            "Missing OAuth credentials. Set GOOGLE_OAUTH_TOKEN_JSON or "
            "GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET/GOOGLE_OAUTH_REFRESH_TOKEN."
        )

    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError("OAuth credentials are invalid and no refresh token is available.")
        creds.refresh(Request())

    return creds


def build_drive_service(env: dict):
    creds = get_creds(env)
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def parse_folder_id(drive_url: str) -> str:
    parsed = urlparse(drive_url)
    path_parts = [part for part in parsed.path.split("/") if part]

    if "folders" in path_parts:
        index = path_parts.index("folders")
        if index + 1 < len(path_parts):
            return path_parts[index + 1]

    query = parse_qs(parsed.query)
    if "id" in query and query["id"]:
        return query["id"][0]

    raise ValueError("Unable to parse Google Drive folder ID from BASE_DRIVE_URL.")


def extract_scan_date(filename: str) -> tuple[Optional[str], str]:
    stem = Path(filename).stem
    match = DATE_PREFIX_PATTERN.match(stem)
    if match:
        return match.group(1), match.group(2)

    date_only_match = DATE_ONLY_PATTERN.match(stem)
    if date_only_match:
        return date_only_match.group(1), ""

    return None, stem

def run_self_tests() -> None:
    test_cases = [
        ("2025_02_22_foobar.pdf", ("2025_02_22", "foobar")),
        ("2020_01_22.pdf", ("2020_01_22", "")),
        ("no_date_here.pdf", (None, "no_date_here")),
        ("2026_99_99_invalid_but_pattern_match.pdf", ("2026_99_99", "invalid_but_pattern_match")),
    ]

    for filename, expected in test_cases:
        result = extract_scan_date(filename)
        if result != expected:
            raise AssertionError(
                f"extract_scan_date failed for {filename}: expected {expected}, got {result}"
            )

    LOGGER.info("Self-tests passed: extract_scan_date")


def sanitize_filename(name: str, max_len: int = 60) -> str:
    normalized = SAFE_FILENAME_PATTERN.sub("_", name.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = "scanned_document"
    return normalized[:max_len].strip("_")


def pdf_has_extractable_text(pdf_path: Path, min_chars: int = 20) -> bool:
    try:
        reader = PdfReader(str(pdf_path))
        extracted = []
        for page in reader.pages:
            extracted.append(page.extract_text() or "")
            if len("".join(extracted).strip()) >= min_chars:
                return True
        return False
    except Exception:
        return False


def list_top_level_pdfs(service, folder_id: str) -> list[dict]:
    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id,name,parents,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return response.get("files", [])


def download_file(service, file_id: str, destination: Path) -> None:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with destination.open("wb") as file_handle:
        downloader = MediaIoBaseDownload(file_handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def run_ocr(input_pdf: Path, output_pdf: Path) -> None:
    if pdf_has_extractable_text(input_pdf):
        LOGGER.info("Skipping OCR: PDF already has extractable text")
        shutil.copy2(input_pdf, output_pdf)
        return

    commands = [
        [
            "ocrmypdf",
            "--skip-text",
            "--deskew",
            "--rotate-pages",
            "--optimize",
            "1",
            "--output-type",
            "pdf",
            str(input_pdf),
            str(output_pdf),
        ],
        [
            "ocrmypdf",
            "--skip-text",
            "--optimize",
            "0",
            "--output-type",
            "pdf",
            str(input_pdf),
            str(output_pdf),
        ],
    ]

    for index, command in enumerate(commands, start=1):
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as error:
            LOGGER.warning("OCR attempt %s failed with exit code %s", index, error.returncode)

    LOGGER.warning("OCR failed after retries; falling back to original PDF text extraction")
    shutil.copy2(input_pdf, output_pdf)


def extract_text_from_pdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages).strip()


def extract_usage_tokens(completion) -> tuple[int, int]:
    usage = getattr(completion, "usage", None)
    if not usage:
        return 0, 0

    input_tokens = (
        getattr(usage, "input_tokens", None)
        or getattr(usage, "prompt_tokens", None)
        or 0
    )
    output_tokens = (
        getattr(usage, "output_tokens", None)
        or getattr(usage, "completion_tokens", None)
        or 0
    )
    return int(input_tokens), int(output_tokens)


def generate_filename_with_llm(
    source_name: str,
    body_text: str,
    model: str,
    api_key: str,
    usage_tracker: dict,
) -> str:
    client = OpenAI(api_key=api_key)
    extracted_char_count = len(body_text)
    ai_input_body = body_text[:1000]
    sent_char_count = len(ai_input_body)

    LOGGER.info("OCR extracted characters: %s", extracted_char_count)
    LOGGER.info("Characters sent to AI model: %s", sent_char_count)

    if extracted_char_count > 1000:
        LOGGER.info("Input exceeds 1000 characters; sending only the first 1000 characters to AI")

    prompt = (
        "You rename scanned PDFs. Return only a suggested filename (no extension) using "
        "letters, numbers, hyphens, and underscores for spaces. No special characters. Maximum of 80 characters but you dont have to use them all.  "
        "Prefer descriptive but concise names.  "
        "If text contain tax forms, such as k1, 1040, W2, include the tax form name in result.  "
        "If text contains a year, include the year.  "
        "If text refers to gift or charitable contributions, include 'Gift' or 'Charity'.  "
        "If text contains a financial institution, include the institution name.  "
        "If text contains an account number, truncate it to only include the last for digits, for example: 12345-98765 would be x8765.  "
        "If text refer to a Trust then use an abbreviated name for the Trust. "
        "Prioritize naming file using elements in the following order: year, tax form name, other, account number.\n\n"
        f"Original file: {source_name}\n\n"
        f"OCR text:\n{ai_input_body}"
    )

    request_args = {
        "model": model,
        "input": prompt,
    }
    if not model.lower().startswith("gpt-5"):
        request_args["temperature"] = 0.1

    completion = client.responses.create(**request_args)
    input_tokens, output_tokens = extract_usage_tokens(completion)
    usage_tracker["requests"] += 1
    usage_tracker["input_tokens"] += input_tokens
    usage_tracker["output_tokens"] += output_tokens
    LOGGER.info(
        "OpenAI usage for this file: input_tokens=%s output_tokens=%s",
        input_tokens,
        output_tokens,
    )

    content = completion.output_text.strip()
    return sanitize_filename(content)


def log_openai_cost_summary(env: dict, usage_tracker: dict) -> None:
    input_tokens = usage_tracker["input_tokens"]
    output_tokens = usage_tracker["output_tokens"]
    requests_count = usage_tracker["requests"]

    input_rate_per_1m = env["OPENAI_INPUT_COST_PER_1M"]
    output_rate_per_1m = env["OPENAI_OUTPUT_COST_PER_1M"]

    input_cost = (input_tokens / 1_000_000) * input_rate_per_1m
    output_cost = (output_tokens / 1_000_000) * output_rate_per_1m
    total_cost = input_cost + output_cost

    LOGGER.info(
        "OpenAI run summary: requests=%s input_tokens=%s output_tokens=%s",
        requests_count,
        input_tokens,
        output_tokens,
    )
    LOGGER.info(
        "OpenAI estimated cost (USD): input=$%.6f output=$%.6f total=$%.6f",
        input_cost,
        output_cost,
        total_cost,
    )
    if input_rate_per_1m == 0.0 and output_rate_per_1m == 0.0:
        LOGGER.warning(
            "OPENAI_INPUT_COST_PER_1M and OPENAI_OUTPUT_COST_PER_1M are 0.0; cost estimate is $0."
        )


def append_logs_to_drive_file(service, folder_id: str, run_log_text: str) -> None:
    if not run_log_text.strip():
        return

    query = (
        f"name='{LOG_FILE_NAME}' and "
        "mimeType='text/plain' and "
        f"'{folder_id}' in parents and trashed=false"
    )

    response = (
        service.files()
        .list(
            q=query,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=1,
        )
        .execute()
    )
    files = response.get("files", [])

    if files:
        log_file_id = files[0]["id"]
        existing_stream = io.BytesIO()
        request = service.files().get_media(fileId=log_file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(existing_stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        existing_text = existing_stream.getvalue().decode("utf-8", errors="replace")
        separator = "" if existing_text.endswith("\n") or not existing_text else "\n"
        combined_text = f"{existing_text}{separator}{run_log_text}"

        upload_stream = io.BytesIO(combined_text.encode("utf-8"))
        media_body = MediaIoBaseUpload(upload_stream, mimetype="text/plain", resumable=False)
        (
            service.files()
            .update(
                fileId=log_file_id,
                media_body=media_body,
                supportsAllDrives=True,
                body={"name": LOG_FILE_NAME},
            )
            .execute()
        )
    else:
        upload_stream = io.BytesIO(run_log_text.encode("utf-8"))
        media_body = MediaIoBaseUpload(upload_stream, mimetype="text/plain", resumable=False)
        (
            service.files()
            .create(
                body={
                    "name": LOG_FILE_NAME,
                    "mimeType": "text/plain",
                    "parents": [folder_id],
                },
                media_body=media_body,
                supportsAllDrives=True,
                fields="id",
            )
            .execute()
        )


def ensure_renamed_subfolder(service, parent_folder_id: str, destination_subfolder: str) -> str:
    query = (
        f"name='{destination_subfolder}' and mimeType='application/vnd.google-apps.folder' and "
        f"'{parent_folder_id}' in parents and trashed=false"
    )
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = response.get("files", [])
    if files:
        return files[0]["id"]

    create_response = (
        service.files()
        .create(
            body={
            "name": destination_subfolder,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return create_response["id"]


def rename_and_move_file(
    service,
    file_id: str,
    new_base_name: str,
    current_parent_id: str,
    renamed_folder_id: str,
) -> None:
    final_name = f"{new_base_name}.pdf"
    (
        service.files()
        .update(
            fileId=file_id,
            addParents=renamed_folder_id,
            removeParents=current_parent_id,
            supportsAllDrives=True,
            body={"name": final_name},
            fields="id,name",
        )
        .execute()
    )


def is_direct_child_of_folder(service, file_id: str, folder_id: str) -> bool:
    metadata = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,parents,trashed",
            supportsAllDrives=True,
        )
        .execute()
    )
    if metadata.get("trashed"):
        return False
    return folder_id in (metadata.get("parents") or [])


def process_pdf_file(
    service,
    file_data: dict,
    parent_folder_id: str,
    renamed_folder_id: str,
    env: dict,
    usage_tracker: dict,
) -> None:
    file_id = file_data["id"]
    file_name = file_data["name"]
    scan_date, _ = extract_scan_date(file_name)

    initial_parents = file_data.get("parents") or []
    if parent_folder_id not in initial_parents:
        LOGGER.info("Skipping out-of-scope file: %s", file_name)
        return

    if not is_direct_child_of_folder(service=service, file_id=file_id, folder_id=parent_folder_id):
        LOGGER.info("Skipping moved/out-of-scope file before OCR: %s", file_name)
        return

    LOGGER.info("Processing: %s", file_name)

    with tempfile.TemporaryDirectory(prefix="drive_scan_") as temp_dir:
        temp_dir_path = Path(temp_dir)
        original_pdf = temp_dir_path / "original.pdf"
        ocr_pdf = temp_dir_path / "ocr.pdf"

        download_file(service=service, file_id=file_id, destination=original_pdf)
        run_ocr(input_pdf=original_pdf, output_pdf=ocr_pdf)
        body_text = extract_text_from_pdf(ocr_pdf)

    suggested_base = generate_filename_with_llm(
        source_name=file_name,
        body_text=body_text,
        model=env["OPENAI_MODEL"],
        api_key=env["OPENAI_API_KEY"],
        usage_tracker=usage_tracker,
    )

    if scan_date:
        suggested_base = f"{scan_date}__{suggested_base}"

    if not is_direct_child_of_folder(service=service, file_id=file_id, folder_id=parent_folder_id):
        LOGGER.info("Skipping moved/out-of-scope file before rename: %s", file_name)
        return

    rename_and_move_file(
        service=service,
        file_id=file_id,
        new_base_name=suggested_base,
        current_parent_id=parent_folder_id,
        renamed_folder_id=renamed_folder_id,
    )

    LOGGER.info("Renamed + moved: %s -> %s.pdf", file_name, suggested_base)


def read_env() -> dict:
    required = ["BASE_DRIVE_URL", "OPENAI_API_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    oauth_token_json_set = bool(os.getenv("GOOGLE_OAUTH_TOKEN_JSON"))
    oauth_triplet_set = all(
        [
            os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
            os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
            os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"),
        ]
    )

    if not oauth_token_json_set and not oauth_triplet_set:
        raise RuntimeError(
            "Missing Drive OAuth credentials. Set GOOGLE_OAUTH_TOKEN_JSON or "
            "GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET/GOOGLE_OAUTH_REFRESH_TOKEN."
        )

    env = {
        "BASE_DRIVE_URL": os.environ["BASE_DRIVE_URL"],
        "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "OPENAI_INPUT_COST_PER_1M": parse_float_env("OPENAI_INPUT_COST_PER_1M", 0.0),
        "OPENAI_OUTPUT_COST_PER_1M": parse_float_env("OPENAI_OUTPUT_COST_PER_1M", 0.0),
        "DEST_SUBFOLDER": os.getenv("DEST_SUBFOLDER", "RENAMED"),
        "GOOGLE_OAUTH_TOKEN_JSON": os.getenv("GOOGLE_OAUTH_TOKEN_JSON"),
        "GOOGLE_OAUTH_CLIENT_ID": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        "GOOGLE_OAUTH_CLIENT_SECRET": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
        "GOOGLE_OAUTH_REFRESH_TOKEN": os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN"),
    }
    return env


def check_dependencies() -> None:
    if shutil.which("ocrmypdf") is None:
        raise RuntimeError("ocrmypdf is not installed in this container.")


def main() -> None:
    configure_logging()
    LOGGER.info("===== Run started at %s =====", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if os.getenv("RUN_SELF_TESTS", "").lower() in {"1", "true", "yes"}:
        run_self_tests()
        return

    env = read_env()
    check_dependencies()
    service = build_drive_service(env)

    LOGGER.info("Drive auth mode: OAuth via google-api-python-client")

    folder_id = parse_folder_id(env["BASE_DRIVE_URL"])
    LOGGER.info("Folder ID: %s", folder_id)

    files = list_top_level_pdfs(service=service, folder_id=folder_id)
    LOGGER.info("Found %s top-level PDF file(s)", len(files))

    usage_tracker = {"requests": 0, "input_tokens": 0, "output_tokens": 0}

    if not files:
        log_openai_cost_summary(env=env, usage_tracker=usage_tracker)
        try:
            append_logs_to_drive_file(service=service, folder_id=folder_id, run_log_text=get_run_log_text())
            LOGGER.info("Appended run log to Google Drive file: %s", LOG_FILE_NAME)
        except Exception as error:
            LOGGER.exception("Failed to append logs to Google Drive: %s", error)
        return

    renamed_folder_id = ensure_renamed_subfolder(
        service=service,
        parent_folder_id=folder_id,
        destination_subfolder=env["DEST_SUBFOLDER"],
    )

    for file_data in files:
        try:
            process_pdf_file(
                service=service,
                file_data=file_data,
                parent_folder_id=folder_id,
                renamed_folder_id=renamed_folder_id,
                env=env,
                usage_tracker=usage_tracker,
            )
        except Exception as error:
            LOGGER.exception("Failed for %s: %s", file_data.get("name", file_data.get("id")), error)

    log_openai_cost_summary(env=env, usage_tracker=usage_tracker)

    try:
        append_logs_to_drive_file(service=service, folder_id=folder_id, run_log_text=get_run_log_text())
        LOGGER.info("Appended run log to Google Drive file: %s", LOG_FILE_NAME)
    except Exception as error:
        LOGGER.exception("Failed to append logs to Google Drive: %s", error)


if __name__ == "__main__":
    main()