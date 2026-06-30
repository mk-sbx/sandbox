#!/usr/bin/env python3
"""Hermetic tests for handbook.py — stdlib unittest, no network, no live wiki.

Run:  python3 .claude/skills/handbook/scripts/test_handbook.py

Ranking/parsing tests use synthetic in-memory docs. Git-sync tests create
throwaway local git repos in a temp dir and clone/pull between them, so nothing
here touches the network or the real handbook checkout.
"""
import importlib.util
import os
import subprocess
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("handbook", os.path.join(_HERE, "handbook.py"))
hb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hb)


def sec(location, title, text, source="x.md"):
    return {"location": location, "title": title, "text": text, "source": source}


def tops(scored):
    return [d["location"] for _, d in scored]


def open_write(path, content):
    with open(path, "w") as f:
        f.write(content)


def git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


class TestTextHelpers(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(hb.clean("<p>a &amp; b</p>"), "a & b")
        self.assertEqual(hb.clean(None), "")

    def test_tokenize_keeps_identifier_chars(self):
        self.assertEqual(hb.tokenize("CR_3.16.0 Binding!"), ["cr_3.16.0", "binding"])

    def test_slugify(self):
        self.assertEqual(hb.slugify("Hello, World! v0.3"), "hello-world-v03")
        self.assertEqual(hb.slugify("3.1 What a binding is"), "31-what-a-binding-is")


class TestPageUrl(unittest.TestCase):
    def test_directory_url_mapping(self):
        self.assertEqual(hb.page_url("adr/ADR_013_x.md"), "adr/ADR_013_x/")
        self.assertEqual(hb.page_url("conventions.md"), "conventions/")
        self.assertEqual(hb.page_url("README.md"), "")
        self.assertEqual(hb.page_url("tooling/db/README.md"), "tooling/db/")
        self.assertEqual(hb.page_url("canon/CR/CR.md"), "canon/CR/CR/")


class TestParseMarkdown(unittest.TestCase):
    def test_splits_by_heading_with_anchors(self):
        md = "intro\n\n# Title\n\nbody1\n\n## Sub\n\nbody2"
        sections, h1 = hb.parse_markdown(md, "adr/x/")
        self.assertEqual(h1, "Title")
        locs = [s["location"] for s in sections]
        self.assertEqual(locs, ["adr/x/", "adr/x/#title", "adr/x/#sub"])
        bodies = {s["location"]: s["text"] for s in sections}
        self.assertEqual(bodies["adr/x/#title"], "body1")
        self.assertEqual(bodies["adr/x/#sub"], "body2")


class TestRankBM25(unittest.TestCase):
    def test_only_matching_docs(self):
        docs = [sec("a/", "A", "alpha beta"), sec("b/", "B", "gamma")]
        self.assertEqual(tops(hb.rank(docs, "alpha")), ["a/"])

    def test_empty_query(self):
        self.assertEqual(hb.rank([sec("a/", "A", "x")], "  !! "), [])

    def test_length_normalisation_prefers_concise(self):
        short = sec("short/", "S", "needle")
        padded = sec("long/", "L", "needle " + "filler " * 200)
        self.assertEqual(tops(hb.rank([short, padded], "needle"))[0], "short/")

    def test_title_boost(self):
        in_title = sec("t/", "needle here", "needle body alpha")
        body_only = sec("b/", "plain", "needle body alpha")
        self.assertEqual(tops(hb.rank([in_title, body_only], "needle"))[0], "t/")

    def test_rare_term_outranks_common(self):
        docs = [sec(f"c{i}/", f"C{i}", "common common common") for i in range(8)]
        docs.append(sec("rare/", "R", "common rare"))
        self.assertEqual(tops(hb.rank(docs, "common rare"))[0], "rare/")


class TestBuildIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, rel, content):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def test_indexes_md_and_yaml_and_honours_excludes(self):
        self._write("a.md", "# A\n\nalpha\n\n## Sec\n\nbeta")
        self._write("sub/b.yaml", "key: value\nneedle: yes")
        self._write("node_modules/junk.md", "# junk\nshould be ignored")
        self._write("architecture/architecture_diagram/app.md", "# app\nignored mdx app")
        sections, pages = hb.build_index(self.tmp)
        sources = {s["source"] for s in sections}
        self.assertIn("a.md", sources)
        self.assertIn("sub/b.yaml", sources)
        self.assertNotIn("node_modules/junk.md", sources)
        self.assertNotIn("architecture/architecture_diagram/app.md", sources)
        # Markdown page keyed by its MkDocs URL; YAML keyed by repo path.
        self.assertIn("a/", pages)
        self.assertIn("sub/b.yaml", pages)
        # YAML is a single whole-file section, searchable by its content.
        self.assertEqual(tops(hb.rank(sections, "needle"))[0], "sub/b.yaml")


class GitSyncBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.origin = os.path.join(self.tmp, "origin")
        os.makedirs(self.origin)
        git(["init", "-b", "main"], self.origin)
        open_write(os.path.join(self.origin, "a.md"), "# A\n\nalpha")
        git(["add", "-A"], self.origin)
        git(["commit", "-m", "init"], self.origin)
        # Point the module at our throwaway origin + clone path + stamp dir.
        self._save = (hb.REPO, hb.REPO_URL, hb.SYNC_TTL, hb.STAMP_DIR)
        hb.REPO = os.path.join(self.tmp, "clone")
        hb.REPO_URL = self.origin
        hb.SYNC_TTL = 3600
        hb.STAMP_DIR = os.path.join(self.tmp, "stamp")

    def tearDown(self):
        hb.REPO, hb.REPO_URL, hb.SYNC_TTL, hb.STAMP_DIR = self._save


class TestGitSync(GitSyncBase):
    def test_clone_then_fast_path(self):
        repo, status = hb.ensure_repo()
        self.assertEqual(status, "cloned")
        self.assertTrue(os.path.isdir(os.path.join(repo, ".git")))
        _, status2 = hb.ensure_repo()  # within TTL
        self.assertEqual(status2, "fresh")

    def test_force_pull_no_change(self):
        hb.ensure_repo()
        _, status = hb.ensure_repo(force_pull=True)
        self.assertEqual(status, "up-to-date")

    def test_force_pull_detects_new_commit(self):
        hb.ensure_repo()
        open_write(os.path.join(self.origin, "b.md"), "# B\n\nbeta")
        git(["add", "-A"], self.origin)
        git(["commit", "-m", "second"], self.origin)
        _, status = hb.ensure_repo(force_pull=True)
        self.assertEqual(status, "updated")

    def test_offline_pull_falls_back_to_checkout(self):
        repo, _ = hb.ensure_repo()
        # Break the remote so the pull fails; existing checkout must still serve.
        import shutil
        shutil.rmtree(self.origin)
        _, status = hb.ensure_repo(force_pull=True)
        self.assertEqual(status, "offline")
        self.assertTrue(os.path.isfile(os.path.join(repo, "a.md")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
