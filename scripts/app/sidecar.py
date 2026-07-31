"""Render the structured LLM result into the sidecar `.md` (docs/SIDECAR_SPEC.md).

The model returns data; Python owns the formatting so the YAML frontmatter is always
valid and the field names exactly match the spec for downstream parsing.
"""

from __future__ import annotations

import re
from typing import Optional

import yaml


def _yaml_block(data: dict) -> str:
    # sort_keys=False keeps the spec's field order; allow_unicode for names/accents.
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---[ \t]*\n?(.*)$", re.DOTALL)


def read_sidecar(md_path) -> tuple[dict, str]:
    """Parse an existing sidecar into (frontmatter_dict, body_str).

    The body is returned with leading blank lines stripped so that read/write is
    idempotent (write re-adds a single blank line after the closing fence).
    Returns ({}, full_text) if there is no parseable YAML frontmatter. Tolerant of a
    closing ``---`` fence that isn't newline-terminated.
    """
    text = md_path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2).lstrip("\n")


def write_sidecar(md_path, fm: dict, body: str) -> None:
    """Write frontmatter + body back out, preserving field order.

    Always emits ``---\\n<fm>\\n---\\n\\n<body>`` so repeated edits round-trip cleanly.
    """
    md_path.write_text(f"---\n{_yaml_block(fm)}\n---\n\n{body.lstrip(chr(10))}",
                       encoding="utf-8")


def build_frontmatter(result: dict, received_date: Optional[str]) -> dict:
    """Assemble the frontmatter dict in spec order (§4)."""
    return {
        "doc_type": result.get("doc_type", "unknown"),
        "patient": result.get("patient", "unknown"),
        "other_parties": result.get("other_parties", []) or [],
        "document_date": result.get("document_date"),
        "received_date": received_date,
        "tax_year": result.get("tax_year"),
        "identifiers": [
            {
                "kind": i.get("kind", "other"),
                "raw": i.get("raw", ""),
                "canonical": i.get("canonical"),
                "confidence": i.get("confidence", "low"),
            }
            for i in (result.get("identifiers") or [])
        ],
        "amounts": result.get("amounts", []) or [],
        "dates": result.get("dates", []) or [],
        "provider": result.get("provider"),
        "overall_confidence": result.get("overall_confidence", "low"),
        "needs_human_review": bool(result.get("needs_human_review", False)),
    }


def render_sidecar(
    result: dict,
    received_date: Optional[str],
    generator: str,
    generated_on: str,
    ocr_text: str = "",
    max_ocr_chars: int = 0,
) -> str:
    """Return the full sidecar markdown (frontmatter + body).

    The transcript is kept COMPLETE: the model cleans the first ``max_ocr_chars`` of
    OCR (``cleaned_transcript``); any OCR beyond that is appended verbatim so every
    page stays searchable and nothing is dropped. Pass ``ocr_text`` (the full OCR) and
    ``max_ocr_chars`` (how much the model saw) to enable the remainder.
    """
    fm = build_frontmatter(result, received_date)
    if fm["needs_human_review"] and result.get("review_reason"):
        fm["review_reason"] = result["review_reason"]

    parts = ["---", _yaml_block(fm), "---", ""]

    title = result.get("title") or "Scanned document"
    parts.append(f"# {title}")
    parts.append("")
    parts.append(
        f"> Auto-generated from OCR by {generator}, {generated_on}. "
        f"Generated once from an immutable scan; safe to edit by hand (not regenerated)."
    )
    parts.append("")

    parts.append("## Summary")
    parts.append("")
    parts.append(result.get("summary") or "_No summary produced._")
    parts.append("")

    key_facts = result.get("key_facts") or []
    if key_facts:
        parts.append("## Key facts")
        parts.append("")
        parts.extend(f"- {line}" for line in key_facts)
        parts.append("")

    ocr_notes = (result.get("ocr_notes") or "").strip()
    if ocr_notes:
        parts.append("## OCR notes")
        parts.append("")
        parts.append(ocr_notes)
        parts.append("")

    # Transcript: cleaned head (what the model saw) + verbatim remainder so the FULL
    # document is preserved and searchable.
    transcript = (result.get("cleaned_transcript") or "").strip()
    remainder = ""
    if ocr_text and max_ocr_chars and len(ocr_text) > max_ocr_chars:
        remainder = ocr_text[max_ocr_chars:].strip()
    # If the model produced no cleaned transcript, fall back to the full raw OCR.
    if not transcript and ocr_text:
        transcript = ocr_text.strip()
        remainder = ""

    if transcript or remainder:
        parts.append("## Cleaned transcript")
        parts.append("")
        if transcript:
            parts.append(transcript)
            parts.append("")
        if remainder:
            parts.append(
                f"### Raw OCR (remaining {len(remainder):,} chars, uncleaned)")
            parts.append("")
            parts.append(
                f"_The cleaned text above covers the first {max_ocr_chars:,} OCR "
                f"characters the model processed; the rest of the document follows "
                f"verbatim so it stays searchable._")
            parts.append("")
            parts.append(remainder)
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"
