"""Pydantic schema contract for the PDF-to-markdown pipeline.

These models are the contract: every stage produces or consumes them, and the CLI
serializes a subset to YAML. ``ParseConfig`` is pure serializable data, so
``metadata.config_snapshot = config.model_dump()`` round-trips into the sidecar.

The pipeline is scoped to what the policy documents in ``policies/`` actually
need: text-layer extraction, table detection, verification, and the assembled
markdown. Image-only (scanned) pages are detected and flagged, not OCR'd.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PageType(str, Enum):
    """Triage classification of a single page, driving its extraction route."""

    text_native = "text_native"  # has a usable text layer
    scanned = "scanned"          # image-only / no meaningful text layer
    mixed = "mixed"              # some text plus significant image content
    empty = "empty"              # genuinely blank (no text, no images)


class ParseStage(str, Enum):
    """Pipeline stage, used to tag issues and timings."""

    triage = "triage"
    extraction = "extraction"
    verification = "verification"
    assembly = "assembly"


class Severity(str, Enum):
    """Severity of a recorded issue. Only ``error`` can demote run status."""

    warning = "warning"
    error = "error"


class RunStatus(str, Enum):
    """Overall outcome of a parse run."""

    ok = "ok"            # no error-severity issues, output produced
    partial = "partial"  # errors occurred, or no output despite pages existing
    failed = "failed"    # nothing extractable


# ---------------------------------------------------------------------------
# Configuration (every knob; no magic numbers live outside this model)
# ---------------------------------------------------------------------------


class ParseConfig(BaseModel):
    """All tunable parameters. Copied into ``ParseMetadata.config_snapshot``."""

    model_config = ConfigDict(extra="forbid")

    min_text_chars: int = Field(
        default=50, ge=0, description="A page with fewer text-layer chars (and an image) is classified scanned."
    )
    verify_min_chars_per_page: int = Field(
        default=1, ge=0, description="Flag a page that yielded fewer chars than this as suspicious."
    )


# ---------------------------------------------------------------------------
# Diagnostics carried in the result contract
# ---------------------------------------------------------------------------


class ParseIssue(BaseModel):
    """A per-page failure or flag. Never silent; never fatal alone."""

    stage: ParseStage
    severity: Severity
    error_type: str = Field(description="Short machine-ish tag, e.g. 'empty_extraction', 'no_text_layer'.")
    message: str = Field(description="Human-readable explanation.")
    page: int | None = Field(default=None, description="1-based page number, if page-scoped.")
    likely_cause: str | None = Field(default=None, description="Best-guess root cause for the operator.")


class ParseMessage(BaseModel):
    """Operator-facing advisory note (e.g. a low-confidence-scan warning).

    Distinct from ``ParseIssue``: messages are context, not failures.
    """

    level: Literal["info", "warning"] = "info"
    message: str
    page: int | None = None


class PageTriage(BaseModel):
    """Per-page detection result from the triage stage."""

    page: int = Field(description="1-based page number.")
    type: PageType
    char_count: int = Field(ge=0, description="Characters in the page's text layer.")
    image_count: int = Field(ge=0)
    has_text_layer: bool


class ExtractionRoute(BaseModel):
    """Which tool/strategy handled a contiguous range of pages."""

    start_page: int = Field(description="1-based, inclusive.")
    end_page: int = Field(description="1-based, inclusive.")
    page_type: PageType
    strategy: str = Field(description="e.g. 'pymupdf_text', 'pymupdf_text+pdfplumber_tables'.")


class StageTiming(BaseModel):
    """Wall-clock time spent in a stage."""

    stage: ParseStage
    seconds: float = Field(ge=0.0)


# ---------------------------------------------------------------------------
# The return value
# ---------------------------------------------------------------------------


class ParseMetadata(BaseModel):
    """Source info, breakdowns, timings, strategy, and overall status."""

    source: str = Field(description="Filename, or '<bytes>' when parsing in-memory bytes.")
    source_bytes: int | None = Field(default=None, description="Size of the input in bytes.")
    page_count: int = Field(ge=0)
    encrypted: bool = False
    page_type_breakdown: dict[PageType, int] = Field(default_factory=dict)
    routes: list[ExtractionRoute] = Field(default_factory=list)
    scanned_pages: list[int] = Field(default_factory=list, description="1-based image-only pages with no extractable text.")
    stage_timings: list[StageTiming] = Field(default_factory=list)
    status: RunStatus = RunStatus.ok
    config_snapshot: dict = Field(default_factory=dict, description="The resolved ParseConfig, for reproducibility.")


class ParseResult(BaseModel):
    """The single return value of ``parse_pdf``. Fully serializable; writes nothing."""

    extracted: str = Field(description="The assembled markdown.")
    metadata: ParseMetadata
    issues: list[ParseIssue] = Field(default_factory=list)
    messages: list[ParseMessage] = Field(default_factory=list)
