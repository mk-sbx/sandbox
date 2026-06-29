"""Table detection and rendering (pdfplumber -> GitHub-flavored markdown).

Tables carry the directly-testable numbers downstream consumers care about
(housing targets, site allocations, percentages), so we detect them explicitly
rather than letting them collapse into flowed text.

pdfplumber is imported lazily and opened once per document (a ``TableSource``),
then queried per page — keeping memory bounded like the rest of the pipeline.
Detection runs only on pages that yielded text; every page is failure-isolated
by the caller.

Known limitation: a detected table's cell text also appears in the page's flowed
text layer, so the structured table is *appended* after the page body rather than
splicing it in place. The structured form is what downstream parsing should use.
"""

from __future__ import annotations

import logging
from pathlib import Path

_LOG = logging.getLogger("pdf_to_markdown")


class TableSource:
    """Lazily-opened pdfplumber handle providing per-page table extraction.

    Construct from a path or raw bytes. Opening is deferred until the first page
    is requested, so a run that never reaches a table-eligible page pays nothing.
    """

    def __init__(self, *, path: str | Path | None = None, data: bytes | None = None) -> None:
        if (path is None) == (data is None):
            raise ValueError("TableSource needs exactly one of path or data")
        self._path = Path(path) if path is not None else None
        self._data = data
        self._pdf = None  # opened on first use

    def _ensure_open(self):
        if self._pdf is None:
            import io

            import pdfplumber

            if self._path is not None:
                self._pdf = pdfplumber.open(self._path)
            else:
                self._pdf = pdfplumber.open(io.BytesIO(self._data))
        return self._pdf

    def tables_markdown(self, page_index: int) -> list[str]:
        """Return GFM markdown for each non-trivial table on the given 0-based page."""
        pdf = self._ensure_open()
        page = pdf.pages[page_index]
        try:
            raw_tables = page.extract_tables()
        finally:
            # pdfplumber caches per-page geometry; release it to bound memory.
            page.flush_cache()
        markdowns = []
        for table in raw_tables:
            if _is_meaningful(table):
                markdowns.append(_table_to_gfm(table))
        return markdowns

    def close(self) -> None:
        if self._pdf is not None:
            try:
                self._pdf.close()
            except Exception as exc:  # never let cleanup raise
                _LOG.debug("error closing pdfplumber handle: %s", exc)
            self._pdf = None


def _is_meaningful(table) -> bool:
    """True only for a genuine grid: >=2 columns and >=2 rows with multiple cells.

    Reject single-column 'tables' and banners. Real documents put ruled lines in
    headers/footers, and pdfplumber happily reports those as 1-column tables —
    requiring at least two populated columns across at least two rows filters that
    noise while keeping real data grids (allocations, housing numbers, etc.).
    """
    if not table or len(table) < 2:
        return False
    max_width = max(len(row) for row in table)
    rows_with_multiple_cells = sum(
        1 for row in table if sum(1 for cell in row if (cell or "").strip()) >= 2
    )
    return max_width >= 2 and rows_with_multiple_cells >= 2


def _table_to_gfm(rows: list[list]) -> str:
    """Render a list-of-rows table as a GitHub-flavored markdown table."""
    norm = [[_clean_cell(cell) for cell in row] for row in rows]
    width = max(len(row) for row in norm)
    norm = [row + [""] * (width - len(row)) for row in norm]

    header, body = norm[0], norm[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _clean_cell(cell) -> str:
    """Flatten a cell to a single markdown-table-safe line."""
    text = (cell or "").replace("\n", " ").replace("|", "\\|")
    return " ".join(text.split())
