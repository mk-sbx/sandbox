"""CLI wrapper: the only place that touches the filesystem.

Maps a few command-line flags onto a ``ParseConfig``, calls the pure
``parse_pdf``, and serializes the result to a file pair:

    <name>.md    -> result.extracted (the assembled markdown)
    <name>.yaml  -> metadata + issues + messages (the diagnostics)

Run:
    python -m tools.pdf_to_markdown INPUT.pdf -o OUTPUT_BASE [flags]

Dependencies: pymupdf, pdfplumber, pyyaml. See requirements.txt.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from .core import enable_verbose, parse_pdf
from .schemas import ParseConfig, ParseResult


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.verbose:
        enable_verbose(logging.DEBUG if args.verbose > 1 else logging.INFO)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"error: input not found: {pdf_path}", file=sys.stderr)
        return 2
    if pdf_path.is_dir():
        print(f"error: expected a single PDF file, not a directory: {pdf_path}", file=sys.stderr)
        return 2

    md_path, yaml_path = _output_paths(pdf_path, args.output)
    result = parse_pdf(str(pdf_path), _config_from_args(args))  # writes nothing
    _write_outputs(result, md_path, yaml_path)

    status = result.metadata.status.value
    print(
        f"{status}: {pdf_path.name} -> {md_path.name}, {yaml_path.name} "
        f"({result.metadata.page_count} pages, {len(result.issues)} issues, "
        f"{len(result.metadata.scanned_pages)} scanned)",
        file=sys.stderr,
    )
    return 0 if status != "failed" else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.pdf_to_markdown",
        description="Convert a single PDF to markdown with diagnostics.",
    )
    parser.add_argument("pdf", help="Path to a single input PDF file.")
    parser.add_argument(
        "-o", "--output",
        help="Output base path or directory. Defaults to the input path's stem. "
             "'.md' and '.yaml' are appended.",
    )
    parser.add_argument("--min-text-chars", type=int, default=None,
                        help="A page below this text-layer char count (with an image) is classified scanned.")
    parser.add_argument("--verify-min-chars", type=int, default=None,
                        help="Flag pages yielding fewer chars than this.")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v for info, -vv for debug logging.")
    return parser


def _config_from_args(args) -> ParseConfig:
    """Map only the flags the user actually set onto ParseConfig (keep defaults otherwise)."""
    overrides: dict = {}
    if args.min_text_chars is not None:
        overrides["min_text_chars"] = args.min_text_chars
    if args.verify_min_chars is not None:
        overrides["verify_min_chars_per_page"] = args.verify_min_chars
    return ParseConfig(**overrides)


def _output_paths(pdf_path: Path, output: str | None) -> tuple[Path, Path]:
    if output is None:
        base = pdf_path.with_suffix("")
    else:
        out = Path(output)
        base = out / pdf_path.stem if out.is_dir() else out
    return base.with_suffix(".md"), base.with_suffix(".yaml")


def _write_outputs(result: ParseResult, md_path: Path, yaml_path: Path) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(result.extracted, encoding="utf-8")
    sidecar = {
        "metadata": result.metadata.model_dump(mode="json"),
        "issues": [i.model_dump(mode="json") for i in result.issues],
        "messages": [m.model_dump(mode="json") for m in result.messages],
    }
    yaml_path.write_text(yaml.safe_dump(sidecar, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
