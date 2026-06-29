"""Tests for the pdf_to_markdown pipeline.

Two tiers (mirroring the ``geo_check`` convention — pytest-discoverable ``test_*``
functions plus a standalone ``_run_all()`` runner):

  - Fast unit tests build tiny PDFs in memory with PyMuPDF (deterministic, no
    fixture files).
  - Integration tests run against the real PDFs in ``policies/`` and are skipped
    when those files are absent.

Run either way:
    pytest tools/tests/test_pdf_to_markdown.py
    python tools/tests/test_pdf_to_markdown.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import fitz  # noqa: E402

from tools.pdf_to_markdown import ParseConfig, RunStatus, parse_pdf  # noqa: E402
from tools.pdf_to_markdown import tables, triage, verify  # noqa: E402
from tools.pdf_to_markdown.extraction import PageExtract  # noqa: E402
from tools.pdf_to_markdown.schemas import PageTriage, PageType  # noqa: E402

_POLICIES = _REPO_ROOT / "policies"
_NPPF = _POLICIES / "NPPF_December_2024.pdf"
_TELFORD = _POLICIES / "telford_and_wrekin_local_plan_2011_2031_adopted_jan_2018.pdf"


class _Skipped(Exception):
    pass


def _skip(reason: str):
    try:
        import pytest

        pytest.skip(reason)
    except ImportError:
        raise _Skipped(reason)


# ---------------------------------------------------------------------------
# Tiny PDF builders
# ---------------------------------------------------------------------------


def _text_pdf_bytes(pages: list[str]) -> bytes:
    doc = fitz.open()
    for body in pages:
        doc.new_page().insert_text((72, 72), body, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_pdf_bytes(text: str = "Scanned content 12345", dpi: int = 200) -> bytes:
    """A single image-only page (no text layer) — triage classifies it scanned."""
    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 100), text, fontsize=22)
    pix = page.get_pixmap(dpi=dpi)
    src.close()

    out = fitz.open()
    page = out.new_page(width=pix.width * 72 / dpi, height=pix.height * 72 / dpi)
    page.insert_image(page.rect, pixmap=pix)
    data = out.tobytes()
    out.close()
    return data


def _triage(page=1, type=PageType.text_native, chars=200, images=0, has_text=True):
    return PageTriage(page=page, type=type, char_count=chars, image_count=images, has_text_layer=has_text)


# ---------------------------------------------------------------------------
# Config + triage classification
# ---------------------------------------------------------------------------


def test_config_defaults():
    config = ParseConfig()
    assert config.min_text_chars == 50
    assert config.verify_min_chars_per_page == 1
    assert set(config.model_fields) == {"min_text_chars", "verify_min_chars_per_page"}


def test_config_rejects_unknown_knob():
    try:
        ParseConfig(not_a_real_knob=1)
    except Exception:
        return
    raise AssertionError("ParseConfig should forbid unknown fields")


def test_triage_classification():
    text_doc = fitz.open(stream=_text_pdf_bytes(["lots of readable policy text here"]), filetype="pdf")
    scan_doc = fitz.open(stream=_scanned_pdf_bytes(), filetype="pdf")
    try:
        text_triage, _ = triage.triage_document(text_doc, ParseConfig())
        scan_triage, scan_msgs = triage.triage_document(scan_doc, ParseConfig())
    finally:
        text_doc.close()
        scan_doc.close()
    assert text_triage[0].type is PageType.text_native and text_triage[0].has_text_layer
    assert scan_triage[0].type is PageType.scanned and not scan_triage[0].has_text_layer
    assert any("scanned" in m.message for m in scan_msgs)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_parse_text_native_end_to_end():
    data = _text_pdf_bytes(["Policy BE10 first page.", "Second page body.", "Third."])
    result = parse_pdf(data)
    assert result.metadata.status is RunStatus.ok
    assert result.metadata.page_count == 3
    assert result.metadata.scanned_pages == []
    assert "Policy BE10" in result.extracted
    assert result.metadata.source == "<bytes>"
    assert result.metadata.page_type_breakdown.get(PageType.text_native) == 3
    assert result.metadata.config_snapshot["min_text_chars"] == 50
    assert result.metadata.stage_timings  # stages were timed


def test_scanned_page_flagged_not_extracted():
    data = _scanned_pdf_bytes()
    result = parse_pdf(data)
    assert result.metadata.scanned_pages == [1]
    assert any(i.error_type == "no_text_layer" for i in result.issues)
    # Single scanned page yields no text -> partial, not failed.
    assert result.metadata.status is RunStatus.partial
    assert all(i.severity.value == "warning" for i in result.issues)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_verify_flags_empty_expected_text():
    extract = PageExtract(page=1, text="", triage=_triage(has_text=True), strategy="pymupdf_text")
    issues = verify.verify_pages([extract], ParseConfig())
    assert any(i.error_type == "empty_extraction" for i in issues)


def test_verify_flags_scanned_page():
    extract = PageExtract(page=1, text="", triage=_triage(type=PageType.scanned, chars=0, images=1, has_text=False), strategy="pymupdf_text")
    issues = verify.verify_pages([extract], ParseConfig())
    assert any(i.error_type == "no_text_layer" for i in issues)


def test_verify_flags_encoding_garbage():
    extract = PageExtract(page=1, text="�����������������", triage=_triage(), strategy="pymupdf_text")
    issues = verify.verify_pages([extract], ParseConfig())
    assert any(i.error_type == "encoding_garbage" for i in issues)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_table_to_gfm_renders_grid():
    md = tables._table_to_gfm([["A", "B"], ["1", "2"], ["3", "4"]])
    lines = md.splitlines()
    assert lines[0] == "| A | B |"
    assert lines[1] == "| --- | --- |"
    assert "| 1 | 2 |" in lines


def test_table_meaningfulness_filters_noise():
    assert tables._is_meaningful([["A", "B"], ["1", "2"]]) is True
    assert tables._is_meaningful([["just one column"], ["another"]]) is False  # 1 col
    assert tables._is_meaningful([["A", "B"]]) is False  # single row
    assert tables._is_meaningful([["", ""], ["", ""]]) is False  # empty


def test_table_cells_escape_pipes():
    assert "a\\|b" in tables._table_to_gfm([["a|b", "c"], ["d", "e"]])


# ---------------------------------------------------------------------------
# Integration against the real policy PDFs
# ---------------------------------------------------------------------------


def test_real_nppf_text_native():
    if not _NPPF.exists():
        _skip(f"missing {_NPPF}")
    result = parse_pdf(str(_NPPF))
    assert result.metadata.status is RunStatus.ok
    assert result.metadata.page_count > 50
    assert result.metadata.scanned_pages == []
    assert "National Planning Policy Framework" in result.extracted


def test_real_telford_tables_detected():
    if not _TELFORD.exists():
        _skip(f"missing {_TELFORD}")
    result = parse_pdf(str(_TELFORD))
    assert result.metadata.page_count > 100
    assert any(r.strategy.endswith("pdfplumber_tables") for r in result.metadata.routes), \
        "expected at least one detected table in the Telford plan"


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = skipped = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS {test.__name__}")
        except _Skipped as exc:
            skipped += 1
            print(f"  SKIP {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
