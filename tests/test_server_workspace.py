"""Tests for server repository mirrors and isolated workspaces."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from trie_remote.job_store import JobSpec, OverlayManifest
from trie_remote.server_paths import ServerPaths
from trie_remote.server_workspace import (
    ensure_bare_repository,
    prepare_all_workspaces,
    prepare_workspace,
)


def git(cwd: Path, *arguments: str) -> str:
    """Run Git for a fixture and return stdout."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ServerWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.paths = ServerPaths.from_root(self.base / "server")
        self.paths.create()
        self.source = self.base / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.email", "fixture@example.com")
        git(self.source, "config", "user.name", "Fixture")
        (self.source / "tracked.txt").write_text("initial\n", encoding="utf-8")
        (self.source / "deleted.txt").write_text("delete me\n", encoding="utf-8")
        git(self.source, "add", ".")
        git(self.source, "commit", "-m", "initial")
        self.commit = git(self.source, "rev-parse", "HEAD")
        self.mirror = ensure_bare_repository(self.paths, "trie-vms")
        git(self.source, "push", str(self.mirror), "HEAD:refs/trie-jobs/alpha/primary")

    def test_prepares_detached_worktree_at_exact_commit(self) -> None:
        workspace = prepare_workspace(
            self.paths,
            job_id="alpha",
            repository="trie-vms",
            commit=self.commit,
            role="primary",
        )

        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.commit)
        self.assertTrue((workspace / ".git").is_file())
        self.assertTrue(workspace.is_relative_to(self.paths.workspaces))

    def test_repeat_preparation_resets_dirty_overlay(self) -> None:
        workspace = prepare_workspace(
            self.paths,
            "alpha",
            "trie-vms",
            self.commit,
            "primary",
        )
        (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (workspace / "new.txt").write_text("new\n", encoding="utf-8")
        (workspace / "deleted.txt").unlink()
        self.assertIn("tracked.txt", git(workspace, "status", "--porcelain=v1"))

        repeated = prepare_workspace(
            self.paths,
            "alpha",
            "trie-vms",
            self.commit,
            "primary",
        )

        self.assertEqual(git(repeated, "status", "--porcelain=v1"), "")
        self.assertEqual(
            (repeated / "tracked.txt").read_text(encoding="utf-8"),
            "initial\n",
        )
        self.assertTrue((repeated / "deleted.txt").exists())
        self.assertFalse((repeated / "new.txt").exists())

    def test_prepare_all_applies_reserved_deletions_idempotently(self) -> None:
        workspace = self.paths.workspaces / "trie-vms/alpha/primary"
        spec = JobSpec(
            job_id="alpha",
            repository="trie-vms",
            workspace=str(workspace),
            workspaces={"primary": str(workspace)},
            includes={},
            weight="light",
            argv=("true",),
            created_at="2026-08-25T00:00:00+00:00",
            commits={"primary": self.commit},
            overlays={
                "primary": OverlayManifest(delete=("deleted.txt",)),
            },
        )

        prepared = prepare_all_workspaces(self.paths, spec)
        self.assertEqual(prepared, {"primary": workspace})
        self.assertFalse((workspace / "deleted.txt").exists())
        (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        repeated = prepare_all_workspaces(self.paths, spec)
        self.assertEqual(
            (repeated["primary"] / "tracked.txt").read_text(encoding="utf-8"),
            "initial\n",
        )
        self.assertFalse((repeated["primary"] / "deleted.txt").exists())

    def test_rejects_unsafe_identifiers_before_creating_workspace(self) -> None:
        for job_id, role in (("../escape", "primary"), ("alpha", "../role")):
            with self.subTest(job_id=job_id, role=role), self.assertRaises(ValueError):
                prepare_workspace(
                    self.paths,
                    job_id,
                    "trie-vms",
                    self.commit,
                    role,
                )

        self.assertFalse((self.paths.workspaces / "escape").exists())


if __name__ == "__main__":
    unittest.main()
