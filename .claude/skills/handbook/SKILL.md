---
name: handbook
description: Search and read the Sandbox Engineering Handbook — the company's source of truth for the SAFE assessment platform. Use whenever building or designing a tool/feature and you need requirements, schemas, conventions, or the rationale behind a design — and ALWAYS when the prompt mentions any of these: SAFE, FEG (Functional Equivalence Group), binding, evaluator, canonical value space, provenance, jurisdiction, sector, ECAT, QT (Question Template), JM/SJM (Jurisdiction Module), the Canon (CR_3.x.x) or ADRs, or any CR_/ADR_/AP_ identifier. Examples — "what is a FEG", "how does binding resolution work", "find the jurisdiction module schema", "why was Route B chosen", "what does the canon say about evaluators". Backed by a git clone of the source repo, so it returns full-fidelity Markdown plus the YAML schemas the rendered wiki omits.
---

# Sandbox Engineering Handbook connector

The handbook is a plain-Markdown git repo
(`github.com/camelops-sandbox/sandbox-engineering-handbook`) also published as an
MkDocs site. This skill works off a **local git clone of the source** — full
Markdown fidelity (tables, code, frontmatter) plus the `.yaml` schemas and ECAT
instances the rendered site filters out. See `SPEC.md` for the decisions and
rationale.

## Configure (env, all optional)

- `HANDBOOK_REPO_URL` — git remote to clone. Default: the GitHub repo.
- `HANDBOOK_REPO_LOCAL_CACHE` — local clone path. Default `~/.cache/handbook/repo`.
  (Point this at a managed cache path, **not** a working dev checkout.)
- `HANDBOOK_URL` — base URL for citation deep-links to the deployed wiki.
- `HANDBOOK_SYNC_TTL` — seconds before a lazy `git pull` (default 3600; `0` =
  pull every call).

Set in `.claude/settings.json` `env`, or your shell.

## Commands

Run with `python3 .claude/skills/handbook/scripts/handbook.py <cmd>`:

- `search "<query>" [-n N] [--full]` — BM25 full-text search; ranked results with source path, citation, snippet. `--full` prints the whole matching section.
- `page <location>` — print a whole file as **raw Markdown/YAML**. Accepts a full path, a page URL (`adr/ADR_013_…/`), or a unique fragment (`ADR_013`).
- `list [section]` — list files; optional section filter (`canon`, `adr`, `tooling`, `agent-prompts`, `architecture`, `instances`).
- `check` — `git pull` now and report HEAD / freshness / index counts.
- `refresh` — force a `git pull`.

## Sync & freshness (how content updates are tracked)

The clone is kept current **against the git remote**, not on a blind timer:

1. **Within `HANDBOOK_SYNC_TTL`** — the local clone is used directly, no network.
2. **After the TTL** — the next command does `git pull --ff-only` (incremental;
   only the deltas transfer). `check` reports whether new commits arrived.
3. **Best-effort** — a failed pull (offline / unauthenticated) warns and falls
   back to the existing checkout, so search never hard-fails.
4. **Manual** — `refresh` forces a pull (use right after canon is pushed);
   `check` reports HEAD + last commit on demand.

Because a skill only runs when invoked, the *periodic* part can't live inside
it. Lazy pull-on-use (above) needs no extra infrastructure; if you want zero
search-time pull latency, move the pull to a `SessionStart` hook or an OS
scheduler (see `SPEC.md` D2).

## How to use it (workflow)

1. **Orient**: `list canon` / `list adr` / `list tooling`.
2. **Find**: `search "<topic>"` to locate the relevant record(s).
3. **Read**: `page <path>` to pull the full record before relying on it.
4. **Cite**: quote the `CR_/ADR_/AP_` identifier and the printed citation.
   Treat the canon (`canon/`, `CR_3.x.x`) as **requirements** and ADRs as the
   **rationale** behind them.

## What's in the handbook

| Section | What it holds |
|---|---|
| `canon/` (CR_3.x.x) | Product Canon — SAFE definitions, runtime architecture, schemas, FEG library, sector typology, jurisdiction config. **Requirements.** Each record has a `CHANGELOG`. |
| `adr/` (ADR_*) | Architecture Decision Records — the *why* behind designs. **Context/rationale.** |
| `tooling/` | QT / JM / SJM / ECAT builder prompts + schemas. |
| `agent-prompts/` (AP_*) | Versioned agent prompt specs and schemas. |
| `architecture/` | Audits, migration checklists, verification & DB reports, phase plans. |
| `instances/` | Concrete ECAT research-runner instances (incl. `.yaml`). |
| `conventions.md` | Naming, versioning, and format rules. |

Out of scope (lives in Google Drive, not here): CR_0/1/2/4/5 — theses, customer
ICPs, design partners, GTM/pricing, corporate.

## Ranking

`search` ranks sections with **BM25** (Okapi, Lucene-style non-negative idf),
document-length normalisation, and an additive title-match bonus so
`CR_/ADR_/AP_` identifier and title hits stay on top. Tunable via env:

- `HANDBOOK_BM25_K1` (default `1.5`) — term-frequency saturation.
- `HANDBOOK_BM25_B` (default `0.5`) — length normalisation (0 none .. 1 full).
- `HANDBOOK_TITLE_BOOST` (default `2.0`) — additive idf-weighted title bonus.

## Tests

`scripts/test_handbook.py` — hermetic stdlib `unittest`, no network or live wiki
(git-sync tests use throwaway local repos). Covers BM25 ranking, Markdown
section parsing, MkDocs URL derivation, slugify, `build_index` (md + yaml +
exclusions), and the git sync state machine (clone, TTL fast-path,
pull-detects-commit, offline fallback). Run:

```
python3 .claude/skills/handbook/scripts/test_handbook.py
```

## Notes

- Markdown is indexed by heading section; `.yaml` schemas are indexed whole-file.
- `page` prints raw source (full Markdown/YAML), so tables/code survive intact.
- Requires only Python 3 stdlib + `git`.
