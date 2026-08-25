"""Tests for sparse local overlays and contained server deletions."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from trie_remote.overlay import apply_overlay_deletions, validate_overlay_path
from trie_remote.repository import RepositoryState


def git(cwd: Path, *arguments: str) -> str:
    """Run Git in one fixture repository."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class OverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.repository = self.base / "trie-space"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.email", "fixture@example.com")
        git(self.repository, "config", "user.name", "Fixture")
        for name in ("changed.txt", "removed.txt", "renamed.txt"):
            (self.repository / name).write_text(f"{name}\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "initial")
        self.excludes = self.base / "sync-excludes.txt"
        self.excludes.write_text(
            "- .git\n- node_modules/\n- .env\n- .env.*.local\n- build/\n",
            encoding="utf-8",
        )

    def test_clean_worktree_has_an_empty_overlay(self) -> None:
        state = RepositoryState.discover(self.repository)

        self.assertEqual(state.overlay(self.excludes).transfer, ())
        self.assertEqual(state.overlay(self.excludes).delete, ())

    def test_discovers_modified_untracked_deleted_and_renamed_paths(self) -> None:
        (self.repository / "changed.txt").write_text("changed\n", encoding="utf-8")
        (self.repository / "new file.txt").write_text("new\n", encoding="utf-8")
        (self.repository / "removed.txt").unlink()
        git(self.repository, "mv", "renamed.txt", "new-name.txt")

        manifest = RepositoryState.discover(self.repository).overlay(self.excludes)

        self.assertEqual(
            manifest.transfer,
            ("changed.txt", "new file.txt", "new-name.txt"),
        )
        self.assertEqual(manifest.delete, ("removed.txt", "renamed.txt"))

    def test_excludes_credentials_dependencies_and_build_outputs(self) -> None:
        for name in (".env", ".env.test.local", "node_modules/pkg.js", "build/out.js"):
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("excluded\n", encoding="utf-8")
        (self.repository / "included.txt").write_text("included\n", encoding="utf-8")

        manifest = RepositoryState.discover(self.repository).overlay(self.excludes)

        self.assertEqual(manifest.transfer, ("included.txt",))

    def test_untracked_symlink_is_transferred_as_a_path(self) -> None:
        os.symlink("changed.txt", self.repository / "link.txt")

        manifest = RepositoryState.discover(self.repository).overlay(self.excludes)

        self.assertEqual(manifest.transfer, ("link.txt",))

    def test_rejects_changed_submodule_overlay(self) -> None:
        child = self.base / "child"
        child.mkdir()
        git(child, "init", "-b", "main")
        git(child, "config", "user.email", "fixture@example.com")
        git(child, "config", "user.name", "Fixture")
        (child / "README.md").write_text("child\n", encoding="utf-8")
        git(child, "add", ".")
        git(child, "commit", "-m", "initial")
        git(
            self.repository,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child),
            "vendor/child",
        )
        git(self.repository, "commit", "-am", "add submodule")
        (self.repository / "vendor/child/README.md").write_text(
            "changed\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "submodule overlay"):
            RepositoryState.discover(self.repository).overlay(self.excludes)

    def test_validates_relative_overlay_paths(self) -> None:
        self.assertEqual(validate_overlay_path("src/file name.py"), "src/file name.py")
        for value in ("", "/tmp/file", "../file", "src/../file", "a//b", "a\nb"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_overlay_path(value)

    def test_deletions_are_contained_and_do_not_follow_parent_symlinks(self) -> None:
        workspace = self.base / "workspace"
        workspace.mkdir()
        (workspace / "remove.txt").write_text("remove\n", encoding="utf-8")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        os.symlink(outside, workspace / "escape")

        apply_overlay_deletions(workspace, ("remove.txt",))
        self.assertFalse((workspace / "remove.txt").exists())
        with self.assertRaises(ValueError):
            apply_overlay_deletions(workspace, ("escape/keep.txt",))
        self.assertTrue((outside / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
