#!/usr/bin/env python3
"""
handbook.py — search and read the Sandbox Engineering Handbook from a local
**git clone** of the source repo (not the rendered MkDocs site).

Why git, not the MkDocs search_index.json (see SPEC.md for the full rationale):
the index is a derived, HTML-stripped, heading-chunked artefact that *excludes*
the YAML schemas and tooling the wiki filters out. Cloning the source gives
full-fidelity Markdown (tables, code, frontmatter), the machine-readable
`.yaml` schemas, file/version identifiers, per-record CHANGELOGs, and git
history — and syncs incrementally (deltas) instead of re-downloading a monolith.

Config (env, all optional):
  HANDBOOK_REPO_URL          git remote to clone.  default: the GitHub repo
  HANDBOOK_REPO_LOCAL_CACHE  local clone path.     default: ~/.cache/handbook/repo
  HANDBOOK_URL               base URL for citation links to the deployed wiki pages
  HANDBOOK_SYNC_TTL          seconds before a lazy `git pull` (default 3600; 0 = every call)
  HANDBOOK_BM25_K1 / _B / HANDBOOK_TITLE_BOOST   ranking knobs (see SPEC.md)

Usage:
  handbook.py search "<query>" [-n N] [--full]
  handbook.py page <location>      # e.g. ADR_013  or  adr/ADR_013_.../  or a path
  handbook.py list [section]       # canon | adr | tooling | agent-prompts | ...
  handbook.py check                # pull now and report HEAD / freshness
  handbook.py refresh              # force a git pull
"""
import argparse
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from html import unescape

# --- configuration -------------------------------------------------------------
DEFAULT_REPO_URL = "https://github.com/camelops-sandbox/sandbox-engineering-handbook.git"
REPO_URL = os.environ.get("HANDBOOK_REPO_URL", DEFAULT_REPO_URL)
REPO = os.environ.get(
    "HANDBOOK_REPO_LOCAL_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "handbook", "repo")
)
# Base URL for clickable citations to the deployed wiki (Markdown pages only).
SITE_URL = os.environ.get(
    "HANDBOOK_URL", "https://camelops-sandbox.github.io/sandbox-engineering-handbook/"
)
SYNC_TTL = int(os.environ.get("HANDBOOK_SYNC_TTL", "3600"))  # seconds
STAMP_DIR = os.environ.get(
    "HANDBOOK_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "handbook")
)

# BM25 ranking parameters (Okapi BM25, Lucene-style non-negative idf). b is below
# the usual 0.75 because canonical sections are often long *because* they are
# dense, not padded. TITLE_BOOST keeps CR_/ADR_/AP_ identifier + heading hits on top.
BM25_K1 = float(os.environ.get("HANDBOOK_BM25_K1", "1.5"))
BM25_B = float(os.environ.get("HANDBOOK_BM25_B", "0.5"))
TITLE_BOOST = float(os.environ.get("HANDBOOK_TITLE_BOOST", "2.0"))

# Directories never indexed: VCS/build noise and the separate Explorer MDX app
# (architecture/architecture_diagram is a React project, not canon).
EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", "site", ".github", ".idea", ".cursor"}
EXCLUDE_SUBTREES = {os.path.join("architecture", "architecture_diagram")}
INDEX_EXTS = {".md", ".yaml", ".yml"}

_WORD = re.compile(r"[a-z0-9_.]+")
_TAG = re.compile(r"<[^>]+>")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


# --- text helpers --------------------------------------------------------------
def clean(text):
    """Drop any stray inline HTML and unescape entities (Markdown is mostly plain)."""
    return unescape(_TAG.sub("", text or ""))


def tokenize(s):
    return _WORD.findall(s.lower())


def slugify(text):
    """Approximate MkDocs/toc heading anchors for citation fragments."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower())
    s = re.sub(r"[ _]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def page_url(relpath):
    """Map a repo-relative Markdown path to its MkDocs directory-URL page path.

    adr/ADR_013_x.md            -> adr/ADR_013_x/
    conventions.md              -> conventions/
    README.md / index.md        -> parent (root for top-level README)
    """
    parts = relpath.replace(os.sep, "/").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    if stem.lower() in ("readme", "index"):
        page = "/".join(parts[:-1])
    else:
        page = "/".join(parts[:-1] + [stem])
    return (page + "/") if page else ""


# --- git sync ------------------------------------------------------------------
def _git(args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )


def _head(repo):
    r = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
    return r.stdout.strip() if r.returncode == 0 else None


def _stamp_path():
    key = re.sub(r"[^a-z0-9]+", "_", REPO.lower()).strip("_")
    return os.path.join(STAMP_DIR, key + ".pullstamp")


def _touch_stamp():
    os.makedirs(STAMP_DIR, exist_ok=True)
    with open(_stamp_path(), "w") as f:
        f.write(str(int(time.time())))


def _stamp_age():
    try:
        return time.time() - os.path.getmtime(_stamp_path())
    except OSError:
        return float("inf")


def ensure_repo(force_pull=False):
    """Clone on first use, else lazily `git pull` once the sync TTL has elapsed.

    Returns (repo_path, status): cloned | updated | up-to-date | fresh | offline.
    Pull is best-effort: on any failure the existing checkout is used so search
    never hard-fails when offline or unauthenticated.
    """
    if not os.path.isdir(os.path.join(REPO, ".git")):
        os.makedirs(os.path.dirname(REPO) or ".", exist_ok=True)
        r = _git(["clone", REPO_URL, REPO])
        if r.returncode != 0:
            sys.exit(f"error: git clone {REPO_URL} failed:\n{r.stderr.strip()}")
        _touch_stamp()
        return REPO, "cloned"

    if not force_pull and _stamp_age() < SYNC_TTL:
        return REPO, "fresh"  # within TTL — skip the network round-trip

    before = _head(REPO)
    r = _git(["pull", "--ff-only"], cwd=REPO)
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        sys.stderr.write(f"warning: git pull failed ({last}); using existing checkout\n")
        return REPO, "offline"
    _touch_stamp()
    return REPO, ("updated" if before != _head(REPO) else "up-to-date")


# --- indexing ------------------------------------------------------------------
def iter_files(repo):
    """Yield (relpath, abspath) for indexable .md/.yaml files, honouring excludes."""
    for root, dirs, files in os.walk(repo):
        rel_root = os.path.relpath(root, repo)
        rel_root = "" if rel_root == "." else rel_root
        if any(rel_root == s or rel_root.startswith(s + os.sep) for s in EXCLUDE_SUBTREES):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in INDEX_EXTS:
                rel = os.path.join(rel_root, f) if rel_root else f
                yield rel.replace(os.sep, "/"), os.path.join(root, f)


def parse_markdown(text, page):
    """Split a Markdown doc into heading-delimited sections.

    Returns (sections, page_title) where each section is {location, title, text}.
    location is page or page#slug; page_title is the first H1 (or the page path).
    """
    sections = []
    page_title = None
    cur_title, cur_slug, buf = None, None, []

    def flush():
        body = "\n".join(buf).strip()
        loc = page if cur_slug is None else f"{page}#{cur_slug}"
        sections.append({"location": loc, "title": cur_title or page or "(root)", "text": body})

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            buf = []
            cur_title = m.group(2).strip()
            cur_slug = slugify(cur_title)
            if page_title is None and m.group(1) == "#":
                page_title = cur_title
        else:
            buf.append(line)
    flush()
    return sections, page_title


def build_index(repo):
    """Walk the clone once. Returns (sections, pages).

    sections — list of {location, title, text, source} for BM25 search.
    pages    — dict location -> {title, source} for list/page (one per file;
               Markdown keyed by its MkDocs page URL, YAML by its repo path).
    """
    sections, pages = [], {}
    for rel, abspath in iter_files(repo):
        try:
            text = open(abspath, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if rel.endswith((".yaml", ".yml")):
            # Schemas etc. — index whole-file; location is the repo-relative path.
            sections.append({"location": rel, "title": rel, "text": text, "source": rel})
            pages[rel] = {"title": rel, "source": rel}
        else:
            page = page_url(rel)
            secs, h1 = parse_markdown(text, page)
            for s in secs:
                s["source"] = rel
                sections.append(s)
            pages[page] = {"title": h1 or page or "(root)", "source": rel}
    return sections, pages


# --- ranking -------------------------------------------------------------------
def rank(sections, query):
    """BM25-rank sections for a query string. Returns [(score, section), ...] desc.

    Pure function (no I/O) so the ranking stays unit-testable.
    """
    qset = set(tokenize(query))
    if not qset:
        return []
    df = defaultdict(int)
    doc_tokens = []
    total_len = 0
    for d in sections:
        toks = tokenize(d.get("title", "") + " " + d.get("text", ""))
        doc_tokens.append(toks)
        total_len += len(toks)
        for t in set(toks) & qset:
            df[t] += 1
    N = len(sections)
    avgdl = (total_len / N) if N else 1.0
    idf = {t: math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5)) for t in qset}

    scored = []
    for d, toks in zip(sections, doc_tokens):
        dl = len(toks)
        if not dl:
            continue
        tf = defaultdict(int)
        for t in toks:
            tf[t] += 1
        title_toks = set(tokenize(d.get("title", "")))
        denom = BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl)
        score = 0.0
        for t in qset:
            f = tf[t]
            if not f:
                continue
            score += idf[t] * (f * (BM25_K1 + 1)) / (f + denom)
            if t in title_toks:
                score += TITLE_BOOST * idf[t]
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return scored


# --- commands ------------------------------------------------------------------
def _cite(loc):
    """A citation string: deployed URL for Markdown pages, repo path for YAML."""
    return f"{loc}  (yaml)" if loc.endswith((".yaml", ".yml")) else f"{SITE_URL}{loc}"


def cmd_search(args, sections, pages):
    if not tokenize(args.query):
        sys.exit("error: empty query")
    scored = rank(sections, args.query)
    if not scored:
        print(f"No matches for: {args.query}")
        return
    print(f"# {len(scored)} matches for: {args.query}\n")
    for _, d in scored[: args.n]:
        print(f"## {d.get('title', '(untitled)')}")
        print(f"  source : {d['source']}")
        print(f"  cite   : {_cite(d['location'])}")
        text = re.sub(r"\s+", " ", clean(d.get("text", ""))).strip()
        if args.full:
            print("\n" + text + "\n")
        else:
            print(f"  text   : {text[:280]}{'…' if len(text) > 280 else ''}")
        print()


def cmd_page(args, sections, pages):
    target = args.location.strip("/").split("#")[0]
    # exact page-location, else unique substring of the location or source path.
    match = None
    for loc, p in pages.items():
        if loc.strip("/") == target:
            match = (loc, p)
            break
    if not match:
        cands = [(loc, p) for loc, p in pages.items()
                 if target in loc or target in p["source"]]
        if len(cands) == 1:
            match = cands[0]
        elif len(cands) > 1:
            print("Ambiguous; candidates:")
            for loc, p in sorted(cands):
                print("  ", p["source"])
            return
    if not match:
        sys.exit(f"error: no page matching '{args.location}'")
    loc, p = match
    repo, _ = ensure_repo.cached  # set by main()
    abspath = os.path.join(repo, p["source"])
    print(f"# {p['title']}")
    print(f"source: {p['source']}")
    print(f"cite  : {_cite(loc)}\n")
    sys.stdout.write(open(abspath, encoding="utf-8", errors="replace").read())


def cmd_list(args, sections, pages):
    section = args.section.strip("/") if args.section else None
    rows = []
    for loc, p in pages.items():
        top = p["source"].split("/")[0] if "/" in p["source"] else "(root)"
        if section and top != section:
            continue
        rows.append((p["source"], p["title"]))
    if not rows:
        tops = sorted({p["source"].split("/")[0] if "/" in p["source"] else "(root)"
                       for p in pages.values()})
        print(f"No pages under '{section}'. Sections: {', '.join(tops)}")
        return
    for src, title in sorted(rows):
        print(f"{src:62s} {title}")
    print(f"\n{len(rows)} files")


def cmd_check(repo, status, sections, pages):
    head = _head(repo)
    log = _git(["log", "-1", "--format=%h  %cd  %s", "--date=short"], cwd=repo)
    msg = {
        "cloned": "freshly cloned",
        "updated": "UPDATED — new commits pulled",
        "up-to-date": "up to date (no new commits)",
        "fresh": f"within sync TTL ({SYNC_TTL}s) — not pulled",
        "offline": "could not pull — using existing checkout",
    }.get(status, status)
    n_md = sum(1 for p in pages.values() if not p["source"].endswith((".yaml", ".yml")))
    n_yaml = len(pages) - n_md
    print(f"Repo   : {repo}")
    print(f"Remote : {REPO_URL}")
    print(f"Result : {msg}")
    print(f"HEAD   : {log.stdout.strip() if log.returncode == 0 else head}")
    print(f"Indexed: {n_md} markdown pages, {n_yaml} yaml files, {len(sections)} sections")


# --- entrypoint ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Search/read the Sandbox Engineering Handbook.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="BM25 full-text search")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=8, help="max results (default 8)")
    s.add_argument("--full", action="store_true", help="print full section text")
    pg = sub.add_parser("page", help="print a whole file (raw Markdown/YAML)")
    pg.add_argument("location")
    ls = sub.add_parser("list", help="list files, optionally by top-level section")
    ls.add_argument("section", nargs="?")
    sub.add_parser("check", help="git pull now and report HEAD / freshness")
    sub.add_parser("refresh", help="force a git pull")
    args = ap.parse_args()

    repo, status = ensure_repo(force_pull=args.cmd in ("check", "refresh"))
    ensure_repo.cached = (repo, status)  # used by cmd_page to locate files
    sections, pages = build_index(repo)

    if args.cmd == "check":
        cmd_check(repo, status, sections, pages)
    elif args.cmd == "refresh":
        print(f"git pull {repo}: {status} (HEAD {_head(repo)}).")
    elif args.cmd == "search":
        cmd_search(args, sections, pages)
    elif args.cmd == "page":
        cmd_page(args, sections, pages)
    elif args.cmd == "list":
        cmd_list(args, sections, pages)


if __name__ == "__main__":
    main()
