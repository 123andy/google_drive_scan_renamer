"""Text extraction from scans.

Strategy (mirrors the proven plumbing from the old google_drive_scan_renamer):
  - PDF with a real text layer  -> extract directly with pypdf (no OCR, free/fast).
  - Image-only PDF              -> ocrmypdf --skip-text, then extract.
  - Bare image (jpg/png/tiff)   -> ocrmypdf turns it into a searchable PDF, then extract.

ocrmypdf + tesseract + ghostscript live in the Docker image, so the host needs no
OCR toolchain installed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

LOGGER = logging.getLogger("scansnap")

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def pdf_has_extractable_text(pdf_path: Path, min_chars: int = 20) -> bool:
    try:
        reader = PdfReader(str(pdf_path))
        acc = []
        for page in reader.pages:
            acc.append(page.extract_text() or "")
            if len("".join(acc).strip()) >= min_chars:
                return True
        return False
    except Exception:
        return False


def extract_text_from_pdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(pages).strip()


def _run_ocrmypdf(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as err:
        LOGGER.warning("ocrmypdf failed (exit %s): %s", err.returncode,
                       (err.stderr or "").strip().splitlines()[-1:] or "")
        return False


def _ocr_pdf(input_pdf: Path, work_dir: Path) -> str:
    """OCR an image-only PDF and return its extracted text."""
    out_pdf = work_dir / "ocr.pdf"
    attempts = [
        ["ocrmypdf", "--skip-text", "--deskew", "--rotate-pages",
         "--optimize", "1", "--output-type", "pdf", str(input_pdf), str(out_pdf)],
        ["ocrmypdf", "--skip-text", "--optimize", "0",
         "--output-type", "pdf", str(input_pdf), str(out_pdf)],
    ]
    for cmd in attempts:
        if _run_ocrmypdf(cmd):
            return extract_text_from_pdf(out_pdf)
    # Last resort: whatever (likely empty) text the original carries.
    LOGGER.warning("OCR failed after retries; falling back to raw extraction")
    return extract_text_from_pdf(input_pdf)


def _ocr_image(input_image: Path, work_dir: Path, image_dpi: int) -> str:
    """OCR a bare image by letting ocrmypdf wrap it into a searchable PDF."""
    out_pdf = work_dir / "ocr.pdf"
    cmd = ["ocrmypdf", "--image-dpi", str(image_dpi),
           "--rotate-pages", "--output-type", "pdf",
           str(input_image), str(out_pdf)]
    if _run_ocrmypdf(cmd):
        return extract_text_from_pdf(out_pdf)
    return ""


def extract_text(path: Path, image_dpi: int = 300) -> str:
    """Return the best available text for a scan, OCR'ing when necessary."""
    ext = path.suffix.lower()
    with tempfile.TemporaryDirectory(prefix="scansnap_ocr_") as tmp:
        work = Path(tmp)
        if ext in PDF_EXTS:
            if pdf_has_extractable_text(path):
                LOGGER.info("Text layer present; skipping OCR")
                return extract_text_from_pdf(path)
            LOGGER.info("No text layer; running OCR")
            # ocrmypdf reads the source in place; copy in so we never touch the original.
            local = work / "input.pdf"
            shutil.copy2(path, local)
            return _ocr_pdf(local, work)
        if ext in IMAGE_EXTS:
            LOGGER.info("Image scan; running OCR")
            local = work / ("input" + ext)
            shutil.copy2(path, local)
            return _ocr_image(local, work, image_dpi)
    raise ValueError(f"Unsupported file type for OCR: {path.name}")
