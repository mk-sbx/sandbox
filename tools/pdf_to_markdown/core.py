"""The orchestrator: ``parse_pdf``.

The single public entry point. It is **pure**: it returns a ``ParseResult`` and
writes nothing. File I/O (reading the path, writing the .md/.yaml pair) lives only
in the CLI, so the same call is safe in a server, a notebook, or a test.

Flow: open (lazy) -> triage (whole doc, cheap) -> extract every page
(text + tables) -> verify -> assemble -> build metadata. Each stage is timed;
per-page failures are isolated into ``issues`` and never abort.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from . import extraction, tables, triage, verify
from .schemas import (
    ExtractionRoute,
    PageType,
    ParseConfig,
    ParseMetadata,
    ParseResult,
    ParseStage,
    RunStatus,
    Severity,
    StageTiming,
)

# Silent by default (NullHandler); the CLI opts in via enable_verbose.
_LOG = logging.getLogger("pdf_to_markdown")
_LOG.addHandler(logging.NullHandler())


def enable_verbose(level: int = logging.INFO) -> None:
    """Attach a stderr handler so the library logs. Idempotent; called by the CLI."""
    _LOG.setLevel(level)
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        for h in _LOG.handlers
    ):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        _LOG.addHandler(handler)


def parse_pdf(pdf: str | Path | bytes, config: ParseConfig | None = None) -> ParseResult:
    """Parse a PDF into markdown, returning a fully-populated ``ParseResult``.

    ``pdf`` is a filesystem path (str/Path) or raw PDF bytes. Never raises for
    per-page problems — those become ``issues``; it may raise only for a wholesale
    failure to open the input.
    """
    config = config or ParseConfig()
    timer = _StageTimer()

    source, source_bytes, doc = _open(pdf)
    _LOG.info("parse start: source=%s bytes=%s", source, source_bytes)
    table_source = _make_table_source(pdf)

    try:
        encrypted = bool(doc.is_encrypted)
        if encrypted:
            doc.authenticate("")  # try empty password; pages stay unreadable if it fails
        page_count = doc.page_count

        with timer.stage(ParseStage.triage):
            triages, messages = triage.triage_document(doc, config)

        with timer.stage(ParseStage.extraction):
            extracts, issues = extraction.extract_pages(doc, triages, table_source)
        with timer.stage(ParseStage.verification):
            issues += verify.verify_pages(extracts, config)

        with timer.stage(ParseStage.assembly):
            extracted = "\n\n".join(e.text.strip("\n") for e in extracts if e.text.strip())

        metadata = _build_metadata(
            source, source_bytes, page_count, encrypted, triages, extracts, extracted, issues, config, timer
        )
        _LOG.info(
            "parse done: status=%s pages=%d issues=%d scanned=%d",
            metadata.status.value, page_count, len(issues), len(metadata.scanned_pages),
        )
        return ParseResult(extracted=extracted, metadata=metadata, issues=issues, messages=messages)
    finally:
        doc.close()
        table_source.close()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _build_metadata(source, source_bytes, page_count, encrypted, triages, extracts, extracted, issues, config, timer) -> ParseMetadata:
    breakdown: dict[PageType, int] = defaultdict(int)
    for t in triages:
        breakdown[t.type] += 1

    routes = extraction.routes_for(extracts)
    scanned_pages = sorted(t.page for t in triages if t.type is PageType.scanned)

    return ParseMetadata(
        source=source,
        source_bytes=source_bytes,
        page_count=page_count,
        encrypted=encrypted,
        page_type_breakdown=dict(breakdown),
        routes=_coalesce_routes(routes),
        scanned_pages=scanned_pages,
        stage_timings=timer.to_list(),
        status=_compute_status(issues, page_count, extracted),
        config_snapshot=config.model_dump(mode="json"),
    )


def _coalesce_routes(routes: list[ExtractionRoute]) -> list[ExtractionRoute]:
    """Merge adjacent per-page routes that share page type + strategy into ranges."""
    coalesced: list[ExtractionRoute] = []
    for route in routes:
        last = coalesced[-1] if coalesced else None
        if last is not None and route.start_page == last.end_page + 1 and route.page_type == last.page_type and route.strategy == last.strategy:
            last.end_page = route.end_page
        else:
            coalesced.append(route.model_copy())
    return coalesced


def _compute_status(issues, page_count, extracted) -> RunStatus:
    """ok = output + no errors; partial = errors, or no output despite pages; failed = no pages."""
    produced = bool(extracted.strip())
    has_error = any(i.severity is Severity.error for i in issues)
    if page_count == 0:
        return RunStatus.failed
    if has_error or not produced:
        return RunStatus.partial
    return RunStatus.ok


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _open(pdf):
    """Open the input lazily. Returns (source_name, size_bytes, doc)."""
    import fitz  # lazy: PyMuPDF only needed when actually parsing

    if isinstance(pdf, (bytes, bytearray)):
        data = bytes(pdf)
        return "<bytes>", len(data), fitz.open(stream=data, filetype="pdf")
    path = Path(pdf)
    return path.name, path.stat().st_size, fitz.open(path)


def _make_table_source(pdf):
    """Build a lazily-opened pdfplumber TableSource from the same input."""
    if isinstance(pdf, (bytes, bytearray)):
        return tables.TableSource(data=bytes(pdf))
    return tables.TableSource(path=Path(pdf))


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


class _StageTimer:
    """Accumulates wall-clock seconds per stage."""

    def __init__(self) -> None:
        self._totals: dict[ParseStage, float] = defaultdict(float)

    @contextmanager
    def stage(self, stage: ParseStage):
        start = time.perf_counter()
        try:
            yield
        finally:
            self._totals[stage] += time.perf_counter() - start

    def to_list(self) -> list[StageTiming]:
        return [StageTiming(stage=s, seconds=round(self._totals[s], 4)) for s in ParseStage if s in self._totals]
