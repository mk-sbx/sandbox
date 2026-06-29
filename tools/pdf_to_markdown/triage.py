"""Stage 1 — Triage: per-page classification.

For each page we cheaply read the text layer and count images, then classify the
page (``PageType``). This drives the page-type breakdown and surfaces image-only
(scanned) pages, which have no extractable text layer.

Triage reads only the text layer (cheap), so running it over the whole document
up front is inexpensive.
"""

from __future__ import annotations

import logging

from .schemas import PageTriage, PageType, ParseConfig, ParseMessage

_LOG = logging.getLogger("pdf_to_markdown")


def triage_document(doc, config: ParseConfig) -> tuple[list[PageTriage], list[ParseMessage]]:
    """Classify every page. Returns (triages, messages)."""
    triages: list[PageTriage] = []
    scanned_pages = 0

    for i in range(doc.page_count):
        page = doc.load_page(i)
        char_count = len(page.get_text("text").strip())
        image_count = len(page.get_images(full=True))

        ptype = _classify(char_count, image_count, config)
        if ptype is PageType.scanned:
            scanned_pages += 1

        triages.append(
            PageTriage(
                page=i + 1,
                type=ptype,
                char_count=char_count,
                image_count=image_count,
                has_text_layer=char_count > 0,
            )
        )
        _LOG.debug("triage page=%d type=%s chars=%d images=%d", i + 1, ptype.value, char_count, image_count)

    messages = []
    if scanned_pages:
        messages.append(
            ParseMessage(
                level="warning",
                message=f"{scanned_pages} image-only (scanned) page(s) have no text layer and were not extracted.",
            )
        )
    return triages, messages


def _classify(char_count: int, image_count: int, config: ParseConfig) -> PageType:
    """Classify a page from its char count and image count."""
    min_chars = config.min_text_chars
    if char_count == 0 and image_count == 0:
        return PageType.empty
    if char_count >= min_chars and image_count > 0:
        return PageType.mixed
    if char_count >= min_chars:
        return PageType.text_native
    if image_count > 0:
        return PageType.scanned
    # Low text, no images: a sparse text page (e.g. a section divider), not a scan.
    return PageType.text_native
