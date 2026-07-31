# Specification — LLM-drafted document sidecar from an OCR stream

**Status:** reference spec (portable). Describes how to instruct an LLM to turn the
OCR text of a scanned document into a structured `.md` "sidecar" summary that sits
next to the original file. Domain-agnostic; the *controlled vocabularies* and *ID
formats* in §7–§8 are the only parts a given domain configures (Aetna shown as the
example).

---

## 1. Purpose

A scanned PDF/image is opaque to search and to downstream agents. The sidecar makes
it **searchable, self-describing, and dedup-able** without re-viewing the image:

- a machine-readable header (YAML frontmatter) of extracted identifiers + metadata
- a short human-readable summary of *what the document is*
- a faithful, OCR-cleaned transcript (optional but recommended)

Because a scanned document is **immutable**, its sidecar is generated **once** and
never needs regeneration — no staleness, safe to cache forever.

## 2. Inputs

| Input | Required | Notes |
|---|---|---|
| `ocr_text` | yes | Raw OCR stream of the document (may be noisy, multi-page). |
| `source_filename` | yes | Original file name; may carry date/type hints. |
| `page_count` | optional | Helps detect multi-document scans. |
| `received_date` | optional | When it arrived / was scanned (≠ document date). |
| `domain_config` | optional | doc-type vocabulary + ID regexes for the domain (§7–§8). |

The LLM must treat `ocr_text` as **untrusted and noisy** (see §6).

## 3. Output

One file: `<source_filename>.md`, placed beside the original. UTF-8. Two parts:
**YAML frontmatter** (the structured contract, §4) then a **Markdown body** (§5).

## 4. Frontmatter schema

```yaml
---
doc_type:            # one value from the controlled vocabulary (§8); "unknown" if unsure
patient:             # primary person the doc concerns; "unknown" / "multiple" allowed
other_parties: []    # providers, payers, other people named
document_date:       # the date printed ON the document (ISO yyyy-mm-dd) or null
received_date:       # from input, or null
tax_year:            # for tax-related docs: the 4-digit TAX YEAR the doc applies to
                     #   (often != document_date); drives an FY<year>_ filename prefix; else null
identifiers:         # high-signal, near-unique IDs — see §7. Each: raw + canonical + confidence
  - kind:            #   e.g. claim_id | check_number | case_number | account | other
    raw:             #   exactly as OCR produced it (verbatim)
    canonical:       #   best-guess corrected value, or null if not confidently correctable
    confidence:      #   high | medium | low
amounts: []          # normalized numbers (no currency symbol/commas), e.g. [123240.00]
dates: []            # all ISO dates found in the body (yyyy-mm-dd)
provider:            # billing/issuing entity if applicable, else null
overall_confidence:  # high | medium | low — the doc as a whole
needs_human_review:  # true | false — true if doc_type unknown, patient ambiguous,
                     #   any critical ID is low-confidence, or the scan is largely illegible
review_reason:       # one line, present only when needs_human_review is true
---
```

Rules:

- Every `identifiers[].raw` is **verbatim OCR** — never "fix" the raw field.
- `canonical` is the corrected value *only when correction is well-justified* (§6); else null.
- Omit/`null` anything not present. Never invent a value to fill a field.

## 5. Body schema

```markdown
# <one-line title: "<doc_type> — <patient> — <document_date>">

> Auto-generated from OCR by <workflow/model>, <generation date>. Generated once
> from an immutable scan; safe to edit by hand (will not be regenerated).

## Summary

<2–4 sentences, plain English, factual. What this document IS, who/what it concerns,
the key fact a human would want (amount, decision, action, status). No speculation;
no derived analysis beyond what the document states.>

## Key facts

- **Type:** …    **Patient:** …    **Date:** …
- **Identifiers:** <claim/check/case IDs, canonical where confident>
- **Amounts:** …    **Provider/Payer:** …
- **Action / status (if stated):** …

## OCR notes

<Anything the reader must know about reliability: illegible regions, low-confidence
IDs (show raw → canonical), ambiguous characters, suspected multi-document scan.>

## Cleaned transcript   (optional but recommended for searchability)

<The OCR text, lightly cleaned: fix obvious OCR artifacts, preserve all numbers/IDs,
keep original line/section structure. This is what makes the scan grep-able.>
```

## 6. Handling the OCR stream (the crux)

The input is OCR, so the LLM both **corrects** and **must not fabricate**. The line
between them:

- **Correct** when a string is obviously a known format with a transcription slip:
  - confusable characters: `O/Q/D↔0`, `I/L↔1`, `S↔5`, `B↔8`, `Z↔2`, `G↔6`, `T↔7`,
    `rn↔m`, `cl↔d`. Apply when the corrected form fits a known ID/word pattern.
  - spacing/casing artifacts, hyphenation across line breaks, stray punctuation.
  - Record the correction: `raw` keeps the OCR string, `canonical` holds the fix,
    `confidence` reflects how sure you are.
- **Do NOT**:
  - invent an identifier, amount, date, or name that is not present in the OCR;
  - "complete" a partially-OCR'd ID by guessing missing characters — if characters
    are missing/illegible, set `canonical: null`, `confidence: low`, and note it;
  - resolve an ambiguity silently — surface it in **OCR notes**.
- **Confidence calibration:** `high` = unambiguous in the OCR; `medium` = corrected a
  known confusable with good format fit; `low` = guessed, partial, or conflicting.
- **Illegibility:** if a critical field can't be read, mark it null + low confidence,
  set `needs_human_review: true`, and say why in `review_reason`.
- **Multi-document scans:** if the OCR clearly contains >1 distinct document, do NOT
  merge them — set `needs_human_review: true`, `review_reason: "multiple documents
  in one scan"`, and summarize each briefly in the body.

## 7. Identifier extraction guidance

Extract **document-specific, near-unique** identifiers (the high-signal anchors used
for dedup and cross-reference). Provide each as raw + canonical + confidence.

Do **not** treat person/account-level numbers (member ID, group number) as
document identifiers — list those under Key facts instead, since they repeat across
every document for that person and would create false duplicate links.

*Domain example (Aetna):*

- `claim_id` — 11 chars, starts `E` or `P`, mixed letters+digits (e.g. `PMTYW30L400`)
- `check_number` — 6–12 digits, usually after "Check No"
- `case_number` — 8–16 digits, usually after "Case number"

## 8. Controlled vocabulary for `doc_type`

Pick exactly one; use `unknown` rather than guessing.

*Domain example (Aetna):* `eob`, `check`, `appeal_decision`, `appeal_form`,
`correspondence`, `plan_document`, `provider_statement`, `medical_record`, `unknown`.
(Other domains substitute their own list; everything else in this spec is unchanged.)

## 9. Guardrails (must-nots)

- Never output a value not grounded in the OCR text.
- Never drop the verbatim `raw` of an identifier.
- Never assert a `doc_type` or `patient` you're not confident in — use `unknown` /
  `multiple` and flag for review.
- Never editorialize in **Summary** beyond what the document states.
- Keep the frontmatter valid YAML and the schema field names exact (downstream code
  parses them).

## 10. Worked example

**Input OCR (noisy):**

```
AETNA  EXPLANATION OF BENEFITS   Member ID W2237806A2
Claim for Marina (spouse)   Provider: The Menninger Clinic
Claim ID PMTYW3OL4O0   Service 01/26/2026 - 04/13/2026
Amount billed $123,24O.OO    Not payable $123,240.00   Refer to Remarks
```

**Output sidecar (`<file>.pdf.md`):**

```markdown
---
doc_type: eob
patient: Marina
other_parties: [The Menninger Clinic]
document_date: null
received_date: 2026-06-24
identifiers:
  - kind: claim_id
    raw: PMTYW3OL4O0
    canonical: PMTYW30L400
    confidence: medium      # corrected O→0 twice; fits the E/P 11-char claim-ID format
amounts: [123240.00]
dates: [2026-01-26, 2026-04-13]
provider: The Menninger Clinic
overall_confidence: medium
needs_human_review: false
---
# EOB — Marina — (date not printed)

> Auto-generated from OCR by <model>, <date>. Generated once from an immutable scan.

## Summary
Aetna Explanation of Benefits for Marina's residential stay at The Menninger Clinic,
service 1/26–4/13/2026, billed $123,240.00 and shown as not payable pending remarks.
This is the Bridge residential claim.

## Key facts
- **Type:** EOB   **Patient:** Marina   **Date:** not printed on doc
- **Identifiers:** claim PMTYW30L400 (OCR'd "PMTYW3OL4O0")
- **Amounts:** $123,240.00 billed / not payable
- **Member ID:** W223780642 (OCR'd "W2237806A2" — person-level, not a doc ID)

## OCR notes
Claim ID read as "PMTYW3OL4O0"; corrected O→0 to "PMTYW30L400" (format-consistent,
medium confidence). Member ID had an `A`→`4` slip. Amount "$123,24O.OO" → 123240.00.

## Cleaned transcript
AETNA Explanation of Benefits. Member ID W223780642. Claim for Marina (spouse).
Provider: The Menninger Clinic. Claim ID PMTYW30L400. Service 01/26/2026–04/13/2026.
Amount billed $123,240.00. Not payable $123,240.00. Refer to Remarks.
```

## 11. Optional post-processing (caller side)

- Validate the frontmatter parses as YAML and field names match this schema.
- Route any sidecar with `needs_human_review: true` to a review queue.
- Feed `identifiers[].canonical` + `amounts` + `dates` into a content-dedup matcher
  (fuzzy, OCR-tolerant) to detect that this scan equals an already-filed digital
  original.
