"""Regression checks using isolated, disposable Mercurial and Git repositories."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("hgit-mirror.py")
SPEC = importlib.util.spec_from_file_location("hgit_mirror", SCRIPT)
mirror_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror_tool)


class RedactionTests(unittest.TestCase):
    def test_literal_case_insensitive_longest_first_and_multiline(self):
        message = "CLIENT PROJECT, client!\n\nhttps://host/preClient/post a+b aab"
        self.assertEqual(
            mirror_tool.redact_message(message, ["client", "Client Project", "a+b"]),
            "***, ***!\n\nhttps://host/pre***/post *** aab",
        )
        self.assertEqual(mirror_tool.redact_message("client", ["client", "*"]), "***")
        self.assertEqual(mirror_tool.redact_message(message), message)

    def test_terms_are_literal_and_blank_terms_are_rejected(self):
        self.assertEqual(mirror_tool.resolve_redactions(["One"], ["One", "Two"]),
                         ["One", "Two"])
        for term in ["", " ", "\t\n"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                mirror_tool.redaction_term(term)

    def test_cross_repo_dates_sort_by_instant(self):
        first = ("2024-01-01 10:00:00+02:00", "a")
        second = ("2024-01-01 09:00:00+00:00", "b")
        self.assertEqual(mirror_tool.merge_commits_preserving_repo_order(
            {"b": [second], "a": [first]}), [first, second])


@unittest.skipUnless(shutil.which("hg") and shutil.which("git"), "Requires hg and git")
class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hgit-mirror-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.mirror = self.base / "mirror"
        self.source.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HGRCPATH": os.devnull,
            "HGPLAIN": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_AUTHOR_NAME": "Test Committer",
            "GIT_AUTHOR_EMAIL": "committer@example.com",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.com",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.command("hg", "init", cwd=self.source)

    def command(self, *args, cwd=None):
        result = subprocess.run(args, cwd=cwd or self.mirror, capture_output=True)
        if result.returncode:
            self.fail(f"{args}: {result.stderr.decode('utf-8', errors='replace')}")
        return result.stdout.decode("utf-8")

    def hg_commit(self, message, files, repo=None):
        repo = repo or self.source
        for name, content in files.items():
            path = repo / name
            if content is None:
                self.command("hg", "remove", name, cwd=repo)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        self.command("hg", "addremove", cwd=repo)
        self.command("hg", "commit", "-m", message, "-u",
                     "Original Author <author@example.com>", "-d", "1700000000 -7200", cwd=repo)
        return self.command("hg", "log", "-r", ".", "-T", "{node}", cwd=repo).strip()

    def sync(self, init=False, exclude=None, redact=None, **options):
        args = argparse.Namespace(
            root=str(self.source), mirror=str(self.mirror), state_file="",
            remote="", recent_commits=0, init=init, dry_run=False,
            exclude=exclude or [], unexclude=[], redact=redact or [],
        )
        vars(args).update(options)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            mirror_tool.sync_mirror(args)
        return output.getvalue()

    def state(self):
        return json.loads((self.mirror / ".git" / mirror_tool.STATE_FILENAME).read_text("utf-8"))

    def messages(self):
        return self.command("git", "log", "--reverse", "--format=%B%x00").split("\0")[:-1]

    def assert_no_empty_commits(self):
        for commit in self.command("git", "rev-list", "HEAD").splitlines():
            changes = self.command("git", "diff-tree", "--root", "--no-commit-id",
                                   "--name-only", "-r", commit)
            self.assertTrue(changes, commit)

    def test_initial_import_skips_empty_and_preserves_sanitized_metadata(self):
        self.hg_commit("Secret initial instructions", {"AGENTS.md": "private\n"})
        message = "Use SECRET project\n\nDetails: https://host/Secret/project a+b."
        self.hg_commit(message, {"code.txt": "public\n", "AGENTS.md": "private 2\n"})
        latest = self.hg_commit("Secret instructions only", {"AGENTS.md": "private 3\n"})
        output = self.sync(init=True, exclude=["AGENTS.md"], redact=["secret", "a+b"])
        self.assertNotIn("secret", output.lower())
        self.assertIn("Applied 1; skipped 2", output)
        self.assertEqual([msg.strip() for msg in self.messages()],
                         ["Use *** project\n\nDetails: https://host/***/project ***."])
        self.assertEqual(self.state()["repos"][""], latest)
        self.assertFalse((self.mirror / "AGENTS.md").exists())
        self.assertEqual(self.command("git", "show", "-s", "--format=%an <%ae>" ).strip(),
                         "Original Author <author@example.com>")
        self.assertEqual(self.command("git", "show", "-s", "--format=%at %aI").strip(),
                         "1700000000 2023-11-15T00:13:20+02:00")
        rows = mirror_tool._short_desc({"": self.source}, self.state())
        self.assertEqual(rows[0][2], "*** instructions only")
        head = self.command("git", "rev-parse", "HEAD")
        self.assertIn("Nothing to sync", self.sync())
        self.assertEqual(self.command("git", "rev-parse", "HEAD"), head)
        self.assert_no_empty_commits()

    def test_incremental_import_masks_bodies_skips_exclusions_and_handles_deletion(self):
        self.hg_commit("Initial", {"code.txt": "one\n", "AGENTS.md": "private\n"})
        self.sync(init=True, exclude=["AGENTS.md", "*/AGENTS.md"], redact=["secret"])
        self.hg_commit("secret only", {"AGENTS.md": "changed\n"})
        self.hg_commit("SECRET mixed\n\nBody with preSecret/post.",
                       {"code.txt": "two\n", "docs/AGENTS.md": "private\n"})
        latest = self.hg_commit("Remove secret code", {"code.txt": None})
        output = self.sync()
        self.assertIn("Applied 2; skipped 1", output)
        self.assertNotIn("secret", output.lower())
        self.assertEqual([msg.strip() for msg in self.messages()],
                         ["Initial", "*** mixed\n\nBody with pre***/post.", "Remove *** code"])
        self.assertEqual(self.state()["repos"][""], latest)
        self.assertEqual(self.command("git", "ls-files"), "")
        self.assertIn("Nothing to sync", self.sync())
        self.assert_no_empty_commits()

    def test_no_included_content_can_later_receive_its_first_commit(self):
        latest = self.hg_commit("Secret only", {"AGENTS.md": "private\n"})
        self.assertIn("Applied 0; skipped 1", self.sync(init=True, exclude=["AGENTS.md"]))
        self.assertEqual(self.state()["repos"][""], latest)
        self.assertEqual(self.command("git", "ls-files"), "")
        self.assertEqual(self.command("git", "for-each-ref"), "")
        self.hg_commit("Public code", {"code.txt": "one\n"})
        self.sync()
        self.assertEqual([msg.strip() for msg in self.messages()], ["Public code"])
        self.assert_no_empty_commits()

    def test_excluded_subrepo_and_parent_pointer_only_commits_are_absent(self):
        secret = self.source / "secret"
        secret.mkdir()
        self.command("hg", "init", cwd=secret)
        self.hg_commit("Secret subrepo", {"private.txt": "private\n"}, repo=secret)
        self.hg_commit("Initial", {".hgsub": "secret = secret\n", "code.txt": "public\n"})
        self.hg_commit("Secret subrepo update", {"private.txt": "changed\n"}, repo=secret)
        # hg records the changed child revision in the parent's .hgsubstate.
        self.hg_commit("Secret parent pointer", {})
        self.sync(init=True, exclude=["secret"], redact=["secret"])
        self.assertEqual([msg.strip() for msg in self.messages()], ["Initial"])
        self.assertNotIn("secret", self.state()["repos"])
        self.assertFalse((self.mirror / "secret").exists())
        self.hg_commit("Secret incremental update", {"private.txt": "again\n"}, repo=secret)
        latest = self.hg_commit("Secret incremental pointer", {})
        self.assertIn("Applied 0; skipped 1", self.sync())
        self.assertEqual(self.state()["repos"][""], latest)
        self.assertEqual([msg.strip() for msg in self.messages()], ["Initial"])
        self.assert_no_empty_commits()

    def test_newly_discovered_empty_subrepo_and_binary_changes(self):
        self.hg_commit("Initial", {"code.txt": "public\n"})
        self.sync(init=True, exclude=["*/AGENTS.md"], redact=["secret"])
        child = self.source / "child"
        child.mkdir()
        self.command("hg", "init", cwd=child)
        child_hash = self.hg_commit("Secret child instructions", {"AGENTS.md": "private\n"}, repo=child)
        self.hg_commit("Secret add child", {".hgsub": "child = child\n"})
        self.assertIn("Applied 0; skipped 2", self.sync())
        self.assertEqual(self.state()["repos"]["child"], child_hash)
        self.hg_commit("Secret binary", {"asset.bin": b"\x00\xff\xfe\n"}, repo=child)
        self.sync()
        self.assertEqual((self.mirror / "child" / "asset.bin").read_bytes(), b"\x00\xff\xfe\n")
        self.assertEqual([msg.strip() for msg in self.messages()], ["Initial", "*** binary"])
        self.assert_no_empty_commits()

    def test_generated_messages_do_not_list_exclusions_and_are_redacted(self):
        self.hg_commit("Initial", {"private.txt": "private\n", "code.txt": "public\n",
                                   ".hgeol": "[patterns]\n** = LF\n"})
        self.sync(init=True, redact=["metadata"])
        self.assertIn("Add Git ***", self.messages()[-1])
        self.sync(exclude=["private.txt"], redact=["exclusions"])
        self.assertEqual(self.messages()[-1].strip(), "Update mirror ***")
        self.assertNotIn("private.txt", self.messages()[-1])
        self.assert_no_empty_commits()

    def test_new_terms_persist_without_pending_commits_and_dry_run_does_not_write(self):
        self.hg_commit("Initial", {"code.txt": "one\n"})
        self.sync(init=True)
        self.sync(redact=["secret"])
        self.assertEqual(self.state()["redact"], ["secret"])
        before = self.state()
        self.hg_commit("SECRET change\n\nSecret details", {"code.txt": "two\n"})
        output = self.sync(dry_run=True, redact=["details"])
        self.assertNotIn("secret", output.lower())
        self.assertNotIn("details", output.lower())
        self.assertEqual(self.state(), before)
        self.assertEqual((self.mirror / "code.txt").read_bytes(), b"one\n")
        self.sync()
        self.assertEqual(self.messages()[-1].strip(), "*** change\n\n*** details")
        preview = self.base / "preview"
        self.sync(init=True, dry_run=True, mirror=str(preview))
        self.assertFalse(preview.exists())

    def test_application_failure_saves_progress_without_advancing_failed_revision(self):
        self.hg_commit("Initial", {"code.txt": "one\n", "other.txt": "one\n"})
        self.sync(init=True)
        success = self.hg_commit("Other change", {"other.txt": "two\n"})
        self.hg_commit("Code change", {"code.txt": "two\n"})
        self.hg_commit("Later change", {"code.txt": "three\n"})
        (self.mirror / "code.txt").write_bytes(b"local conflict\n")
        with self.assertRaises(RuntimeError):
            self.sync()
        self.assertEqual(self.state()["repos"][""], success)
        self.assertEqual(self.messages()[-1].strip(), "Other change")
        self.assertEqual(self.command("git", "diff", "--cached", "--name-only"), "")
        self.command("git", "restore", "--worktree", "--", "code.txt")
        self.sync()
        self.assertEqual((self.mirror / "code.txt").read_bytes(), b"three\n")
        self.assert_no_empty_commits()

    def test_interactive_passes_terms_into_sync(self):
        self.hg_commit("Secret initial", {"code.txt": "one\n"})
        args = argparse.Namespace(root=str(self.source), mirror=str(self.mirror),
                                  state_file="", remote="", recent_commits=0,
                                  exclude=[], unexclude=[], redact=["secret"])
        with mock.patch("builtins.input", side_effect=["1", "0", "", "q"]), \
                mock.patch.object(mirror_tool.os, "system"), \
                contextlib.redirect_stdout(io.StringIO()):
            mirror_tool.run_interactive(args)
        self.assertEqual(self.state()["redact"], ["secret"])
        self.assertEqual(self.messages()[0].strip(), "*** initial")

    def test_cli_rejects_blank_redaction_before_creating_mirror(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.source),
                                 "--mirror", str(self.mirror), "--init", "--redact", " "],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not be blank", result.stderr)
        self.assertFalse(self.mirror.exists())


if __name__ == "__main__":
    unittest.main()
