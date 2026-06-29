"""Robust, observable PDF-to-markdown conversion, scoped to the policy documents.

Public API:

    from tools.pdf_to_markdown import parse_pdf, ParseConfig, ParseResult

``parse_pdf`` is a pure function: it returns a ``ParseResult`` and writes nothing.
File I/O (reading the PDF, writing the ``.md`` / ``.yaml`` pair) lives only in the
CLI (``python -m tools.pdf_to_markdown``).
"""

from __future__ import annotations

from .core import parse_pdf
from .schemas import (
    ExtractionRoute,
    PageTriage,
    PageType,
    ParseConfig,
    ParseIssue,
    ParseMessage,
    ParseMetadata,
    ParseResult,
    ParseStage,
    RunStatus,
    Severity,
    StageTiming,
)

__all__ = [
    "parse_pdf",
    "ParseConfig",
    "ParseResult",
    "ParseMetadata",
    "ParseIssue",
    "ParseMessage",
    "PageTriage",
    "ExtractionRoute",
    "StageTiming",
    "PageType",
    "ParseStage",
    "Severity",
    "RunStatus",
]
