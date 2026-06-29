"""Stage 3 — Extraction: PyMuPDF text layer plus pdfplumber table detection.

Every page's text layer is read with PyMuPDF; pages with content are also scanned
for tables (rendered as GitHub-flavored markdown). Image-only (scanned) pages have
no text layer and simply yield empty text — verification flags them.

A failure on one page is caught, recorded as a ``ParseIssue``, and the page is
kept as an empty slot — one bad page never aborts the document.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .schemas import PageTriage, ParseConfig, ParseIssue, ParseStage, Severity

_LOG = logging.getLogger("pdf_to_markdown")


@dataclass
class PageExtract:
    """Internal per-page extraction product (not part of the public contract)."""

    page: int
    text: str
    triage: PageTriage
    strategy: str


def extract_pages(doc, triages: list[PageTriage], table_source) -> tuple[list[PageExtract], list[ParseIssue]]:
    """Extract every page in order. Returns (page extracts, issues)."""
    extracts: list[PageExtract] = []
    issues: list[ParseIssue] = []

    for triage in triages:
        try:
            extract = _extract_page(doc, triage, issues, table_source)
        except Exception as exc:  # isolate per-page failure
            _LOG.exception("extraction failed on page %d", triage.page)
            issues.append(
                ParseIssue(
                    stage=ParseStage.extraction,
                    severity=Severity.error,
                    error_type="extraction_failed",
                    message=f"{type(exc).__name__}: {exc}",
                    page=triage.page,
                    likely_cause="Corrupt page object or unsupported content stream.",
                )
            )
            extract = PageExtract(page=triage.page, text="", triage=triage, strategy="failed")
        extracts.append(extract)
    return extracts, issues


def _extract_page(doc, triage, issues, table_source) -> PageExtract:
    page = doc.load_page(triage.page - 1)
    markdown = _text_to_markdown(page.get_text("text"))
    strategy = "pymupdf_text"

    if markdown:  # only worth probing for tables on pages that have content
        tables = _extract_tables(table_source, triage.page, issues)
        if tables:
            markdown = _append_tables(markdown, tables)
            strategy = "pymupdf_text+pdfplumber_tables"
    return PageExtract(page=triage.page, text=markdown, triage=triage, strategy=strategy)


def _extract_tables(table_source, page_no, issues) -> list[str]:
    """Detect tables on a text page, failure-isolated. Returns GFM markdown strings."""
    try:
        return table_source.tables_markdown(page_no - 1)
    except Exception as exc:  # degrade to plain text, never crash the page
        _LOG.debug("table extraction failed on page %d: %s", page_no, exc)
        issues.append(
            ParseIssue(
                stage=ParseStage.extraction,
                severity=Severity.warning,
                error_type="table_extraction_failed",
                message=f"Table detection failed; page kept as plain text. {type(exc).__name__}: {exc}",
                page=page_no,
                likely_cause="Complex ruling/borderless layout pdfplumber could not resolve.",
            )
        )
        return []


def _append_tables(markdown: str, tables: list[str]) -> str:
    """Append detected tables under a heading, after the page's flowed text."""
    return markdown + "\n\n**Detected tables:**\n\n" + "\n\n".join(tables)


def routes_for(extracts: list[PageExtract]):
    """One per-page route per extract (coalesced into ranges later, in core)."""
    from .schemas import ExtractionRoute

    return [
        ExtractionRoute(start_page=e.page, end_page=e.page, page_type=e.triage.type, strategy=e.strategy)
        for e in extracts
    ]


_MULTI_BLANK = re.compile(r"\n{3,}")


def _text_to_markdown(text: str) -> str:
    """Light normalization of a text layer into markdown-safe paragraphs.

    Conservative: trims trailing whitespace and collapses runs of blank lines. It
    does not fabricate headings — inventing structure would be lossy.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    return _MULTI_BLANK.sub("\n\n", "\n".join(lines)).strip()
