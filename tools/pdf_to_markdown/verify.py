"""Stage 4 — Verification: sanity-check each page's output, never pass silently.

After extraction, every page is checked against the configured thresholds. A
suspicious result becomes a ``ParseIssue`` — an empty page that should have had
text, an image-only page with no text layer, or encoding garbage is surfaced
rather than quietly flowing into the final markdown.
"""

from __future__ import annotations

from .extraction import PageExtract
from .schemas import PageType, ParseConfig, ParseIssue, ParseStage, Severity

# Below this fraction of replacement/control characters a page is considered clean.
_GARBAGE_RATIO = 0.10


def verify_pages(extracts: list[PageExtract], config: ParseConfig) -> list[ParseIssue]:
    """Check every page extract. Returns issues."""
    issues: list[ParseIssue] = []

    for extract in extracts:
        page_no = extract.page
        char_count = len(extract.text.strip())

        # 1. An image-only page with no text layer (not extracted, no OCR).
        if extract.triage.type is PageType.scanned:
            issues.append(
                ParseIssue(
                    stage=ParseStage.verification,
                    severity=Severity.warning,
                    error_type="no_text_layer",
                    message="Image-only (scanned) page has no text layer; nothing extracted.",
                    page=page_no,
                    likely_cause="A scanned/raster page; its text is not machine-readable without OCR.",
                )
            )
            continue

        # 2. A page that should have had text but came back (near) empty.
        if extract.triage.has_text_layer and char_count < config.verify_min_chars_per_page:
            issues.append(
                ParseIssue(
                    stage=ParseStage.verification,
                    severity=Severity.warning,
                    error_type="empty_extraction",
                    message=f"Page yielded {char_count} chars but text was expected (min {config.verify_min_chars_per_page}).",
                    page=page_no,
                    likely_cause="Unusual encoding or vector-drawn text.",
                )
            )

        # 3. Encoding garbage / mojibake.
        if _looks_like_garbage(extract.text):
            issues.append(
                ParseIssue(
                    stage=ParseStage.verification,
                    severity=Severity.warning,
                    error_type="encoding_garbage",
                    message="Extracted text contains a high ratio of replacement/control chars.",
                    page=page_no,
                    likely_cause="Broken font encoding or a non-embedded custom font.",
                )
            )

    return issues


def _looks_like_garbage(text: str) -> bool:
    """True if the text is mostly replacement chars or non-printable control chars."""
    stripped = text.strip()
    if not stripped:
        return False
    bad = sum(1 for ch in stripped if ch == "�" or (ord(ch) < 32 and ch not in "\t\n\r"))
    return (bad / len(stripped)) > _GARBAGE_RATIO
