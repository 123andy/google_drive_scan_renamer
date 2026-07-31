"""LLM call: OCR text -> structured filename + sidecar fields.

One call produces both deliverables (a descriptive filename and every field the
sidecar needs). We use OpenAI Structured Outputs so the result is guaranteed to
match SIDECAR_SCHEMA; sidecar.py then renders deterministic YAML+Markdown from it.

The correct-but-never-fabricate rules (SIDECAR_SPEC.md §6, §9) live in the prompt.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from openai import OpenAI

from . import config

LOGGER = logging.getLogger("scansnap")

# JSON Schema handed to OpenAI Structured Outputs. Field names mirror the sidecar
# frontmatter (docs/SIDECAR_SPEC.md §4) plus the body sections (§5) and the
# descriptive filename. Strict mode requires every property in `required`; we use
# nullable types for "may be absent".
SIDECAR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "filename_descriptive": {
            "type": "string",
            "description": (
                "Descriptive part of the filename, NO date prefix and NO extension. "
                "Encode when present, in priority order: year, tax-form name "
                "(1040/K-1/W-2/1099...), institution/provider, key parties, and "
                "account numbers truncated to x<last4> (e.g. 12345-98765 -> x8765). "
                "Concise; letters/digits/spaces only -- it will be sanitized."
            ),
        },
        "doc_type": {"type": "string", "enum": config.DOC_TYPES},
        "patient": {
            "type": "string",
            "description": "Primary person the document concerns; 'unknown' or 'multiple' allowed.",
        },
        "other_parties": {"type": "array", "items": {"type": "string"}},
        "document_date": {
            "type": ["string", "null"],
            "description": "Date printed ON the document, ISO yyyy-mm-dd, else null.",
        },
        "tax_year": {
            "type": ["integer", "null"],
            "description": (
                "For a TAX-related document only (tax return, W-2/1099/K-1/1098, "
                "estimated/quarterly tax payment, IRS/FTB notice, property tax bill, "
                "etc.), the TAX YEAR the document applies to as a 4-digit year. This "
                "is OFTEN NOT the document date: a 2025 return filed in 2026 -> 2025; "
                "a Q1-2026 estimated payment confirmed in 2026 -> 2026; a property tax "
                "bill for the 2025-2026 roll -> 2025. null for non-tax documents or "
                "when the tax year genuinely cannot be determined. Do NOT also repeat "
                "this year inside filename_descriptive -- it is added as an FY prefix."
            ),
        },
        "provider": {
            "type": ["string", "null"],
            "description": "Billing/issuing entity if applicable, else null.",
        },
        "identifiers": {
            "type": "array",
            "description": (
                "Document-specific, near-unique IDs only (claim/check/case/invoice/"
                "account...). NOT person-level numbers like member/group ID."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": config.IDENTIFIER_KINDS},
                    "raw": {"type": "string", "description": "Verbatim OCR string -- never corrected."},
                    "canonical": {
                        "type": ["string", "null"],
                        "description": "Corrected value only when well-justified, else null.",
                    },
                    "confidence": {"type": "string", "enum": config.CONFIDENCE_LEVELS},
                },
                "required": ["kind", "raw", "canonical", "confidence"],
            },
        },
        "amounts": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Normalized amounts, no currency symbol/commas (e.g. 123240.00).",
        },
        "dates": {
            "type": "array",
            "items": {"type": "string"},
            "description": "All ISO yyyy-mm-dd dates found in the body.",
        },
        "overall_confidence": {"type": "string", "enum": config.CONFIDENCE_LEVELS},
        "needs_human_review": {"type": "boolean"},
        "review_reason": {
            "type": ["string", "null"],
            "description": "One line; present only when needs_human_review is true.",
        },
        "title": {"type": "string", "description": "One-line: '<doc_type> - <patient> - <date>'."},
        "summary": {"type": "string", "description": "2-4 factual sentences. No speculation."},
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Bullet lines: type, patient, date, identifiers, amounts, provider, action/status, person-level numbers.",
        },
        "ocr_notes": {
            "type": "string",
            "description": "Reliability notes: illegible regions, low-confidence IDs (raw->canonical), ambiguities, suspected multi-document scan.",
        },
        "cleaned_transcript": {
            "type": "string",
            "description": "OCR text lightly cleaned: fix artifacts, preserve all numbers/IDs and structure. Empty string if not useful.",
        },
    },
    "required": [
        "filename_descriptive", "doc_type", "patient", "other_parties",
        "document_date", "tax_year", "provider", "identifiers", "amounts", "dates",
        "overall_confidence", "needs_human_review", "review_reason",
        "title", "summary", "key_facts", "ocr_notes", "cleaned_transcript",
    ],
}

SYSTEM_PROMPT = (
    "You turn the OCR text of a single scanned document into (1) a good descriptive "
    "filename and (2) a structured sidecar summary. Treat the OCR as UNTRUSTED and NOISY.\n\n"
    "Rules:\n"
    "- CORRECT obvious OCR confusable-character slips (O<->0, I/l<->1, S<->5, B<->8, "
    "Z<->2, G<->6, rn<->m) ONLY when the corrected form fits a known word/ID format. "
    "Keep the verbatim OCR string in identifiers[].raw; put the fix in canonical.\n"
    "- NEVER fabricate. Do not invent an identifier, amount, date, or name not present "
    "in the OCR. Do not guess missing characters of a partial ID -- set canonical=null, "
    "confidence=low, and note it in ocr_notes.\n"
    "- identifiers[] holds ONLY document-specific near-unique IDs (claim/check/case/"
    "invoice/account...). Person-level numbers (member ID, group number) go in key_facts, "
    "NOT identifiers[].\n"
    "- TAX YEAR: if the document is tax-related (return, W-2/1099/K-1/1098, estimated or "
    "quarterly tax payment, IRS/FTB notice, property tax bill, etc.), set tax_year to the "
    "year the document APPLIES to -- which is often not its date (a 2025 return filed in "
    "2026 is tax_year 2025; a 2026 estimated payment is 2026). The filename gets an "
    "'FY<year>_' prefix from this automatically, so do NOT also put that year in "
    "filename_descriptive. Use null for non-tax documents.\n"
    "- If doc_type is unclear use 'unknown'; if the patient is ambiguous use 'unknown' or "
    "'multiple'. Set needs_human_review=true (with a one-line review_reason) when doc_type "
    "is unknown, the patient is ambiguous, any critical ID is low-confidence, the scan is "
    "largely illegible, or it clearly contains multiple documents.\n"
    "- summary is factual, 2-4 sentences, no analysis beyond what the document states.\n"
    "- cleaned_transcript: lightly clean the OCR but preserve every number/ID and the "
    "line/section structure; use '' if there is nothing useful to transcribe."
)


def _build_user_prompt(source_name: str, ocr_text: str, max_chars: int) -> str:
    body = ocr_text[:max_chars]
    truncated = " (truncated)" if len(ocr_text) > max_chars else ""
    return (
        f"Original scan filename: {source_name}\n"
        f"OCR character count: {len(ocr_text)}{truncated}\n\n"
        f"--- OCR TEXT ---\n{body}\n--- END OCR TEXT ---"
    )


def analyze_document(
    source_name: str,
    ocr_text: str,
    cfg: config.Config,
) -> tuple[dict, dict]:
    """Return (structured_result, usage) for one document.

    usage: {'input_tokens': int, 'output_tokens': int}
    """
    client = OpenAI(api_key=cfg.openai_api_key)
    user_prompt = _build_user_prompt(source_name, ocr_text, cfg.max_ocr_chars)

    completion = client.chat.completions.create(
        model=cfg.openai_model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "document_sidecar",
                "strict": True,
                "schema": SIDECAR_SCHEMA,
            },
        },
    )

    content = completion.choices[0].message.content or "{}"
    result = json.loads(content)

    usage_obj = getattr(completion, "usage", None)
    usage = {
        "input_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
    }
    LOGGER.info("LLM usage: input=%s output=%s", usage["input_tokens"], usage["output_tokens"])
    return result, usage
