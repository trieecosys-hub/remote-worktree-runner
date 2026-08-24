"""Tests for local Git repository discovery."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from trie_remote.repository import RepositoryState


def git(cwd: Path, *arguments: str) -> str:
    """Run a Git command in a fixture repository."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class RepositoryStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.repository = self.base / "trie-vms"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.email", "fixture@example.com")
        git(self.repository, "config", "user.name", "Fixture")
        (self.repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        git(self.repository, "add", "tracked.txt")
        git(self.repository, "commit", "-m", "initial")

    def test_discovers_normal_repository_and_dirty_state(self) -> None:
        (self.repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.repository / "new.txt").write_text("new\n", encoding="utf-8")

        state = RepositoryState.discover(self.repository / "tracked.txt")

        self.assertEqual(state.name, "trie-vms")
        self.assertEqual(state.root, self.repository.resolve())
        self.assertEqual(state.commit, git(self.repository, "rev-parse", "HEAD"))
        self.assertEqual(state.branch, "main")
        self.assertTrue(state.dirty)

    def test_linked_worktree_keeps_product_repository_name(self) -> None:
        linked = self.base / "feature-worktree"
        git(self.repository, "worktree", "add", "-b", "feature/test", str(linked))

        state = RepositoryState.discover(linked)

        self.assertEqual(state.name, "trie-vms")
        self.assertEqual(state.root, linked.resolve())
        self.assertEqual(state.git_common_dir, (self.repository / ".git").resolve())
        self.assertEqual(state.branch, "feature/test")
        self.assertFalse(state.dirty)

    def test_rejects_repository_outside_allowlist(self) -> None:
        unknown = self.base / "unknown-product"
        unknown.mkdir()
        git(unknown, "init", "-b", "main")
        git(unknown, "config", "user.email", "fixture@example.com")
        git(unknown, "config", "user.name", "Fixture")
        (unknown / "README.md").write_text("fixture\n", encoding="utf-8")
        git(unknown, "add", "README.md")
        git(unknown, "commit", "-m", "initial")

        with self.assertRaises(ValueError):
            RepositoryState.discover(unknown)


if __name__ == "__main__":
    unittest.main()
