# Handbook connector — spec & rationale

The decisions behind the `handbook` skill, and *why* each was made. The skill
gives Claude Code searchable, citable access to the Sandbox Engineering
Handbook so tools are built against the canon (requirements) and ADRs
(rationale) rather than assumptions.

Backend: **git clone of the source repo** (not the rendered
MkDocs site). On hold: FTS5, embeddings, MCP server (see Non-goals).

---

## D1 — Sync from a git clone of the source, not the MkDocs `search_index.json`

**Decision.** Clone `camelops-sandbox/sandbox-engineering-handbook` and index the
raw `.md` and `.yaml` files. Do **not** fetch the published
`…/search/search_index.json`.

**Rationale.** The MkDocs index is a *derived* artefact optimised for the
in-browser search box, and measurably lossy as a knowledge source:

- It is **rendered, HTML-stripped, heading-chunked** text — Markdown tables (58
  source files), code fences with languages (44 files), emphasis and heading
  hierarchy are flattened. The per-page root entry is empty; page text is
  scattered across anchor entries.
- It **excludes** everything non-`.md`: the wiki's `hooks.py` filters out the
  **45 YAML schemas, 16 Python tools, 10 JSON** files. The machine-readable
  QT/JM/SJM/ECAT schemas and ECAT instances are simply *not in the index*.
- It is **monolithic** (~3.3 MB) — any change re-downloads the whole file.

Cloning the source gives full-fidelity Markdown, the YAML schemas + instances
the index hides, file/version identifiers in filenames, per-record
`CHANGELOG.md`, git history/blame ("what changed when, and why"), and
**incremental** sync (deltas, not a monolith). The only things given up are the
prebuilt lunr index (replaced by our own BM25 — better, see D5) and zero-auth
URL access (the clone needs repo credentials once).

## D2 — Lazy pull-on-use with a TTL; best-effort; no daemon or cron

**Decision.** On any command, clone if absent; otherwise `git pull --ff-only`
only once `HANDBOOK_SYNC_TTL` (default 3600 s) has elapsed since the last pull.
A failed pull warns and falls back to the existing checkout. `refresh` forces a
pull; `check` pulls and reports HEAD.

**Rationale.** A skill is **reactive** — it runs when Claude invokes it, and
nothing about a skill ticks on a timer. "Periodic" therefore cannot live
*inside* the skill. Rather than stand up a daemon or cron unit, we mirror the
HTTP conditional-cache pattern the previous backend used: a soft TTL keeps the
common case network-free, and a stale checkout costs one cheap `git pull`.
Best-effort pull means search **never hard-fails** when offline or
unauthenticated — the worst case is slightly stale local content, which the
`check`/`refresh` commands make visible and fixable. If zero search-time pull
latency is ever wanted, promote the pull to a `SessionStart` hook or an OS
scheduler — *out of band*, still not a service.

## D3 — Configurable clone location; data lives outside the skill directory

**Decision.** `HANDBOOK_REPO_LOCAL_CACHE` (default `~/.cache/handbook/repo`) is the clone
path; `HANDBOOK_REPO_URL` the remote. The skill directory holds **code only**.

**Rationale.** A skill should be portable and committable; a git clone + its
index are machine-local mutable *state*. Keeping the clone at a configurable,
gitignored cache path keeps the two cleanly separated and lets the same skill
serve a local dev checkout, a CI clone, or each developer's machine. Never point
`HANDBOOK_REPO_LOCAL_CACHE` at a working dev checkout — the lazy pull could disturb it.

## D4 — Heading-chunked sections for Markdown; whole-file for YAML

**Decision.** Split each `.md` into heading-delimited sections
(`location = page#slug`, `title = heading`); index each `.yaml`/`.yml` as one
whole-file document keyed by its repo path.

**Rationale.** Heading granularity is the right retrieval unit for prose canon —
it returns the relevant *section* of a long CR/ADR, not the whole record, and it
preserves the section→page grouping the `page` command and citations rely on.
Schemas, by contrast, are coherent units that lose meaning when chunked, so they
stay whole. Anchors are slugified to approximate MkDocs' `toc` so citations
deep-link to the right place on the deployed site.

## D5 — BM25 ranking (length-normalised, title-boosted), not plain TF-IDF

**Decision.** Rank with Okapi BM25, Lucene-style non-negative idf,
`k1=1.5`, `b=0.5`, plus an additive idf-weighted title bonus. All env-tunable.

**Rationale.** Two corpus facts make plain TF-IDF mis-rank:
1. **Section length varies enormously** (a one-line `## Summary` vs a 600-word
   `## Argument`). Without length normalisation, long sections win for being
   long — observed: "jurisdiction module schema" surfaced a long research-agent
   prompt above the actual schema. BM25's `b` term fixes this; `b=0.5` (below the
   usual 0.75) penalises length gently because canonical sections are often long
   *because* they are dense, not padded.
2. **Common terms are very common** (`binding`/`feg`/`evaluator` each hit ~20% of
   files). BM25's idf down-weights them so the rare, discriminating query term
   dominates multi-word queries.
The additive title bonus keeps `CR_/ADR_/AP_` identifier and heading matches on
top, which is where most real lookups land. Verified: BM25 corrected the two
queries plain TF-IDF mis-ranked, with no regression on exact-ID lookup.

## D6 — Exclude the Explorer app and build noise from the index

**Decision.** Skip `.git`, `node_modules`, `__pycache__`, `site`, `.github`,
`.idea`, `.cursor`, and the `architecture/architecture_diagram` subtree.

**Rationale.** `architecture_diagram` is a separate React/MDX application (181
tsx, 50 mdx, plus `node_modules`) — ~9 MB of app code, not canon. Indexing it
would bloat the index and pollute results. The audit notes in `architecture/*.md`
are kept; only the app subtree is dropped.

## D7 — Citations mirror the deployed MkDocs page URLs

**Decision.** Map each source path to its MkDocs directory-URL page
(`adr/ADR_013_x.md` → `…/adr/ADR_013_x/`) and print it via `HANDBOOK_URL`. YAML
files cite their repo-relative path (they aren't published pages).

**Rationale.** Searching/reading happens against the local clone, but the user
wants a clickable link to the canonical, deployed record. Reproducing MkDocs'
URL scheme gives deep-links that survive whether the reader opens the wiki or
the repo.

---

## Non-goals (deliberately on hold)

- **SQLite FTS5 / persisted index.** Per-invocation indexing of ~120 Markdown
  files is sub-100 ms today, so an on-disk index isn't worth the complexity yet.
  When the corpus grows enough that re-indexing per query bites, FTS5 (BM25
  built in, incremental) is the drop-in upgrade — `rank()` is already isolated.
- **Embeddings / semantic search.** The canon is dominated by identifier
  vocabulary (`CR_`/`ADR_`/`AP_`) where keyword + BM25 + Claude's iterative
  retrieval wins on effort-adjusted value. Revisit only when real queries show
  keyword precision failing on conceptual lookups.
- **MCP server / daemon.** Justified only by non-Claude consumers, a shared team
  index, or sub-second latency at scale. None apply; a skill + lazy pull +
  in-process BM25 is correctly sized.

## Tests

`scripts/test_handbook.py` — hermetic stdlib `unittest`. Covers BM25 ranking
(length-norm, title boost, rare-vs-common), Markdown section parsing, MkDocs URL
derivation, slugify, `build_index` (md + yaml, exclusions), and the git sync
state machine (clone, TTL fast-path, pull-detects-commit, offline fallback) via
throwaway local git repos — no network, no live wiki.
