# pdf_to_markdown — Spec & Design Rationale

A standalone tool that converts a single PDF into clean markdown plus a structured
diagnostics sidecar. Built to feed `policy_reader` (see `policy_reader/spec.md`),
whose job is to extract actionable policy wording, exact references (e.g.
"Policy BE 10"), GIS pointers, and directly-testable numbers from local plans.

This document records **what the tool does, how it was built, and — importantly —
why it was repeatedly trimmed down** to its current shape.

---

## 1. Purpose & guiding principle

Produce a **faithful, deterministic, offline** markdown rendering of a PDF, plus
machine-readable diagnostics — and stop there. Conversion is kept strictly
separate from *interpretation*: downstream consumers (`policy_reader`, an LLM,
etc.) do the interpreting, deliberately and auditably, over clean source text.

Faithfulness is the non-negotiable. The downstream task hinges on exact wording
and exact numbers ("no more than 10% slope", "Policy BE 10"); a converter that
silently paraphrases, drops a table cell, or rewrites "50%" poisons everything
built on it. So the tool never summarizes and never guesses structure it can't
see.

## 2. Scope (deliberately narrow)

Scoped to what the two documents in `policies/` actually exercise:

- `NPPF_December_2024.pdf` — 82 pages, fully text-native.
- `telford_and_wrekin_local_plan_2011_2031_adopted_jan_2018.pdf` — 201 pages /
  35 MB, mostly text-native with policy tables and one scanned cover page.

The tool was first built as a general, configurable pipeline, then **trimmed in
three passes** to remove everything those two documents don't need (§6).

## 3. Core contract

One pure function — it returns a value and **writes nothing**:

```python
def parse_pdf(pdf: str | Path | bytes, config: ParseConfig | None = None) -> ParseResult
```

All I/O (reading the PDF, writing the `.md`/`.yaml` pair) lives only in the CLI
(`python -m tools.pdf_to_markdown`). This keeps `parse_pdf` safe to call from a
server, notebook, or test with zero side effects, and keeps the result fully
serializable.

`ParseResult` (Pydantic) carries:
- `extracted: str` — the assembled markdown.
- `metadata: ParseMetadata` — source info, page count, `page_type_breakdown`,
  per-range `routes` (which strategy handled which pages), `scanned_pages`,
  per-stage `stage_timings`, overall `status` (`ok`/`partial`/`failed`), and a
  `config_snapshot` for reproducibility.
- `issues: list[ParseIssue]` — per-page failures/flags (stage, severity,
  error_type, message, page, likely_cause).
- `messages: list[ParseMessage]` — operator-facing advisory notes.

`ParseConfig` is pure serializable data (2 knobs): `min_text_chars`,
`verify_min_chars_per_page`. **Schemas were defined and confirmed before building
on them** — they are the contract.

## 4. Pipeline stages

Each stage is distinct, individually testable, timed, and failure-isolated (one
bad page is recorded as an issue, never aborts the run).

1. **Triage** (`triage.py`) — per-page: read the text layer, count images,
   classify `PageType` (text_native / mixed / scanned / empty). Cheap (text only),
   run over the whole document up front to produce the breakdown.
2. **Extraction** (`extraction.py`) — per page: PyMuPDF text layer, plus
   pdfplumber table detection on pages that yielded text.
3. **Verification** (`verify.py`) — sanity-check each page: empty-where-text-
   expected, image-only (scanned) page with no text layer, encoding garbage.
   Failures become issues; nothing passes silently.
4. **Assembly** (`core.py`) — concatenate page markdown in order.

`core.py` orchestrates, builds metadata, computes status, and owns the timing.
`tables.py` renders detected grids as GitHub-flavored markdown. `__main__.py` is
the CLI and the only filesystem-touching code.

## 5. Tables — a real-document tuning note

pdfplumber's default table detection flagged page header/footer banners (ruled
lines) as 1-column "tables", producing junk like
`1\|Telford&WrekinCouncil\|...`. Tuned against the actual Telford plan, the
`_is_meaningful` filter now requires a genuine grid (≥2 columns and ≥2 rows with
multiple filled cells). This removed the false positives and left 8 pages of real
data tables (housing numbers, site allocations). Detected tables are **appended**
under a "Detected tables:" heading after the page's flowed text — the structured
form is what downstream parsing should consume; the flowed copy stays clean.

## 6. What was removed, and why (the trimming trajectory)

The tool started general and was cut down in three explicit passes. Recording the
reasoning because the *removals* are as deliberate as the design.

**Pass 1 — simplify OCR backend.** The original swappable `OCRBackend` Protocol +
registry + injection was over-engineered for the need. Replaced with a direct
Tesseract call (lazy-imported).

**Pass 2 — trim to what the two policies need.**
- **Caching / restartability** (whole `cache.py`): the deliverable runs used
  `--no-cache`; both docs parse in seconds. Restart only earns its keep on
  multi-thousand-page jobs that crash mid-run — not here.
- **Chunking**: its only real purpose was bounding memory and being the cache
  unit. PyMuPDF already loads pages lazily, and without caching, chunks added
  nothing. Replaced with a straight per-page loop.
- **`never`/`always` OCR modes, image extraction, page markers, text-coverage
  threshold, encrypted special-case**: none exercised by the two documents.

**Pass 3 — remove OCR entirely.** OCR was touching **1 page out of 283** (the
Telford cover), and that output was half-garbled decorative text
(`a co-operative Cc oO U N Cc IL`). The actual policy content, tables, and numbers
all live in the text layer. So OCR was not earning its three dependencies
(`pytesseract`, `Pillow`, the system `tesseract` binary). Removed. The scanned
cover is now **detected and flagged** (`scanned_pages: [1]`, a `no_text_layer`
warning) rather than silently dropped or noisily OCR'd.

Net effect: 12 modules → 7, OCR-free, `ParseConfig` from 14 knobs → 2, runtime
dropped, and the NPPF output stayed byte-identical (it never used OCR) while
Telford's only changed by dropping the garbled cover text.

## 7. What was kept (and why it survived trimming)

Robustness and observability were core requirements, so these stayed even as
features were cut:
- The **pure-function + Pydantic contract** and the **md/yaml output pair**.
- **Per-page failure isolation** — never let one page kill the document.
- **Structured diagnostics** (issues/messages) distinct from operator **logging**
  (module logger, `NullHandler` by default, `-v`/`-vv` opt-in).
- **Triage classification + scanned-page flagging** — costs nothing and means a
  scanned page is never silently missing from the output.
- **Table handling** — the one piece of real structure these policy docs need.

## 8. Why a custom tool (vs WebFetch or a library)

- **WebFetch** is the wrong primitive: URL-only (inputs are local 35 MB files),
  context-limited (can't take 201 pages whole), and it returns *a model's reading*
  — lossy and non-reproducible. It conflates convert with interpret, which breaks
  faithfulness.
- **Off-the-shelf converters** (`pymupdf4llm`, `markitdown`, `docling`) are the
  fair comparison and could replace the ~10-line text-extraction core. What they
  don't give: the md/yaml diagnostics contract, the tuned table filtering, and a
  stable seam for `policy_reader`.
- A reasonable future step: keep this scaffolding (config, result, triage, verify,
  tables, CLI) and swap the PyMuPDF `get_text` core for `pymupdf4llm` to get richer
  markdown structure (headings/lists) without losing the contract or determinism.

## 9. Dependencies

`pydantic`, `pymupdf` (fitz), `pdfplumber`, `pyyaml` — pinned in
`requirements.txt`. No OCR deps, no system binaries.

## 10. Usage & outputs

```
python -m tools.pdf_to_markdown INPUT.pdf -o OUTPUT_BASE [--min-text-chars N] [-v]
# writes OUTPUT_BASE.md (extracted markdown) and OUTPUT_BASE.yaml (diagnostics)
```

Generated deliverables live alongside the sources in `policies/` as
`<name>.md` / `<name>.yaml`.

## 11. Testing

`tools/tests/test_pdf_to_markdown.py` — pytest-discoverable plus a standalone
runner (`python tools/tests/test_pdf_to_markdown.py`). Fast unit tests build tiny
PDFs in memory (deterministic, no fixtures); integration tests run against the
real `policies/` PDFs and skip if absent. Covers config/triage/verification/table
rendering/status and the two real documents end-to-end.
