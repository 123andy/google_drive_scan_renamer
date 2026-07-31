"""Configuration, controlled vocabularies, and small filename/date helpers.

This is the only place the *domain* is configured (per docs/SIDECAR_SPEC.md §7-§8):
the doc_type vocabulary and the identifier kinds. Everything else is mechanism.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# --- Controlled vocabulary for doc_type (SIDECAR_SPEC.md §8) --------------------
# Household domain, grounded in a survey of ORGANIZATION AREA + _HOME_: spans
# banking/investments, taxes, insurance/medical, real estate (multiple properties),
# vehicles, legal/identity, and several LLCs/trusts/a foundation. Kept at a sensible
# altitude -- e.g. one "tax_form" with the specific form (W-2/1099/K-1) carried in
# the filename + key facts, rather than a slug per IRS form.
# Pick exactly one; use "unknown" rather than guessing.
DOC_TYPES = [
    # Banking / finance
    "bank_statement",
    "investment_statement",
    "trust_statement",          # B Trust / SDTC quarterly statements
    "loan_statement",           # mortgage / credit line
    "credit_card_statement",
    "account_summary",
    "financial_statement",      # P&L, balance sheet
    "check",
    "wire_transfer",            # wire instructions / confirmations
    # Tax
    "tax_return",               # 1040, 568, LLC/trust returns
    "tax_form",                 # informational: W-2, 1099, K-1, 1098 (form in filename)
    "tax_notice",               # IRS/FTB notices, CP575 (EIN assignment)
    "donation_receipt",         # charitable contributions
    # Insurance / medical
    "eob",                      # explanation of benefits
    "insurance_claim",
    "insurance_policy",         # declarations, renewals, certificates
    "insurance_card",
    "medical_bill",
    "medical_record",
    "appeal_decision",
    "appeal_form",
    # Real estate / property
    "property_tax_bill",
    "property_assessment",
    "deed",                     # deed of trust, reconveyance, grant deed
    "title_report",
    "hoa_statement",            # AMCA Homer assessments / annual summaries
    "permit",                   # tree, alarm, backflow, smoke detector
    "disclosure",               # real estate TDS/PRDS, addenda
    "inspection_report",
    # Vehicles
    "vehicle_registration",
    "vehicle_insurance",
    # Legal / identity
    "trust_document",           # trust agreement, will, distribution notice
    "legal_agreement",          # consulting, NDA, operating agreement
    "court_document",           # summons, interrogatories, verification
    "notary_document",
    "identity_document",        # birth/marriage cert, passport, citizenship
    # Business / org
    "foundation_report",
    "meeting_minutes",
    # General fallbacks
    "invoice",
    "bill",                     # utility / general bill
    "receipt",
    "statement",                # generic statement
    "correspondence",           # generic letter
    "plan_document",
    "report",
    "form",
    "school_document",          # transcripts, programs, enrollment
    "unknown",
]

# --- Identifier kinds (SIDECAR_SPEC.md §7) -------------------------------------
# Near-unique anchors used for content dedup and (future) metadata-based routing.
# Document-specific IDs (claim/check/case/invoice/...) are the strongest dedup keys;
# asset/entity-level IDs (account/policy/parcel/ein) are included because they drive
# routing, but person-level numbers (member/group ID) do NOT belong here -- they
# repeat across every doc for a person and create false duplicate links (-> Key facts).
IDENTIFIER_KINDS = [
    # Document-specific (best dedup keys)
    "claim_id",
    "check_number",
    "case_number",
    "invoice_number",
    "confirmation_number",
    "reference_number",
    "letter_number",        # IRS/FTB notice letter IDs (e.g. CP575)
    "order_number",
    "transaction_id",
    # Asset / entity-level (high-signal for routing)
    "account_number",       # truncated to x<last4> in the filename, full+last4 here
    "policy_number",
    "loan_number",
    "parcel_number",        # property APN (assessor / property tax bills)
    "ein",                  # LLC / trust / foundation tax ID
    "other",
]

CONFIDENCE_LEVELS = ["high", "medium", "low"]

# --- Filename / date helpers (mirrors the old tool's conventions) --------------
DATE_PREFIX_PATTERN = re.compile(r"^(\d{4})_(\d{2})_(\d{2})[_]+(.*)$")
DATE_ONLY_PATTERN = re.compile(r"^(\d{4})_(\d{2})_(\d{2})$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9_]+")

# File extensions the job will pick up out of the inbox root.
SUPPORTED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# Names at the root that are project files / data, never scans to process.
IGNORED_NAMES = {
    "CLAUDE.md",
    "google_drive_scan_renamer.log",
    ".DS_Store",
}


def extract_scan_date(filename: str) -> tuple[Optional[str], str]:
    """Return (YYYY_MM_DD, descriptive_rest) parsed from a scan filename.

    ScanSnap names look like ``2026_06_24_Some Description.pdf``. The date is the
    scan date used as the ``YYYY_MM_DD__`` filename prefix.
    """
    stem = Path(filename).stem
    m = DATE_PREFIX_PATTERN.match(stem)
    if m:
        y, mo, d, rest = m.groups()
        return f"{y}_{mo}_{d}", rest
    m = DATE_ONLY_PATTERN.match(stem)
    if m:
        y, mo, d = m.groups()
        return f"{y}_{mo}_{d}", ""
    return None, stem


def date_prefix_to_iso(date_prefix: Optional[str]) -> Optional[str]:
    """`2026_06_24` -> `2026-06-24`; None passes through."""
    if not date_prefix:
        return None
    return date_prefix.replace("_", "-")


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Collapse to ``[A-Za-z0-9_]`` only, trim, cap length."""
    normalized = SAFE_FILENAME_PATTERN.sub("_", (name or "").strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = "scanned_document"
    return normalized[:max_len].strip("_")


def build_final_basename(scan_date: Optional[str], descriptive: str,
                         tax_year: Optional[int] = None) -> str:
    """``YYYY_MM_DD__[FY<year>_]<descriptive>`` (double underscore after the date).

    For tax-related documents, ``tax_year`` is prefixed as an ``FY<year>_`` token at
    the front of the descriptive part (e.g. ``2026_06_10__FY2026_IRS_EstTaxPayment``).
    """
    if tax_year:
        descriptive = f"FY{tax_year} {descriptive}"  # space -> '_' via sanitize
    desc = sanitize_filename(descriptive)
    if scan_date:
        return f"{scan_date}__{desc}"
    return desc


class Config:
    """Resolved runtime configuration, read from environment."""

    def __init__(self) -> None:
        # Paths are container paths when run via Docker (see docker-compose.yml).
        self.inbox_dir = Path(os.getenv("INBOX_DIR", "/data"))
        self.output_dir = Path(os.getenv("OUTPUT_DIR", "/data/PROCESSED"))
        # Hard failures are quarantined here so the inbox still drains and the
        # cron does not re-bill a poison file forever.
        self.error_dir = self.output_dir / "_errors"
        # Successfully processed but low-confidence docs land here as a review
        # queue (adjudicate.py clears them back to output_dir). Frontmatter's
        # needs_human_review stays the source of truth.
        self.review_dir = self.output_dir / "_review"

        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

        # How much OCR text to hand the model (chars) for analysis + transcript
        # cleaning. OCR beyond this is still preserved verbatim in the sidecar
        # transcript (see sidecar.render_sidecar), so nothing is ever dropped.
        self.max_ocr_chars = int(os.getenv("MAX_OCR_CHARS", "32000"))

        # DPI assumed for bare images that carry no DPI metadata.
        self.image_dpi = int(os.getenv("IMAGE_DPI", "300"))

        self.dry_run = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}

        # State dir (run log + per-file ledger). On Docker this is /work/state
        # (scratch); the host relays the ledger to its own scripts/state.
        self.state_dir = Path(os.getenv("STATE_DIR", str(self.inbox_dir / "scripts" / "state")))
        self.ledger_path = self.state_dir / "ledger.jsonl"

        # USD per 1M tokens, for the cost column. Defaults are gpt-4.1-mini list
        # pricing -- override in .env if you change OPENAI_MODEL or rates move.
        self.input_cost_per_1m = float(os.getenv("OPENAI_INPUT_COST_PER_1M", "0.40"))
        self.output_cost_per_1m = float(os.getenv("OPENAI_OUTPUT_COST_PER_1M", "1.60"))

    def cost_usd(self, in_tokens: int, out_tokens: int) -> float:
        return (in_tokens / 1_000_000) * self.input_cost_per_1m + \
               (out_tokens / 1_000_000) * self.output_cost_per_1m

    def validate(self) -> None:
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set (put it in scripts/.env).")
