"""Tests for exact SSH, Git, and rsync command construction."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from trie_remote.repository import RepositoryState
from trie_remote.transport import Transport


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        stdout = '{"workspace":"/srv/trie-platform/workspaces/trie-space/alpha/primary"}\n'
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class TransportTests(unittest.TestCase):
    def test_sync_policy_excludes_nested_build_caches(self) -> None:
        policy = (Path(__file__).resolve().parents[1] / "config/sync-excludes.txt").read_text()
        self.assertIn("- target/", policy)
        self.assertIn("- node_modules/", policy)
        self.assertNotIn("- /target", policy)

    def setUp(self) -> None:
        self.runner = RecordingRunner()
        self.transport = Transport(
            ssh_alias="trie-docker",
            remote_root=Path("/srv/trie-platform"),
            exclude_file=Path("/tmp/sync excludes.txt"),
            run=self.runner,
        )
        self.state = RepositoryState(
            name="trie-space",
            root=Path("/tmp/source with spaces"),
            git_common_dir=Path("/tmp/source with spaces/.git"),
            commit="a" * 40,
            branch="main",
            dirty=True,
        )

    def test_ssh_keeps_json_in_stdin(self) -> None:
        self.transport.ssh(["start"], input_bytes=b'{"argv":["a b"]}')
        argv, kwargs = self.runner.calls[-1]
        self.assertEqual(
            argv,
            ["ssh", "trie-docker", "/srv/trie-platform/bin/trie-runner", "start"],
        )
        self.assertEqual(kwargs["input"], b'{"argv":["a b"]}')

    def test_push_commit_uses_exact_job_ref(self) -> None:
        self.transport.push_commit(self.state, "alpha", "primary")
        argv, _kwargs = self.runner.calls[-1]
        self.assertEqual(
            argv,
            [
                "git",
                "push",
                "--no-verify",
                "--force",
                "trie-docker:/srv/trie-platform/repos/trie-space.git",
                f"{self.state.commit}:refs/trie-jobs/alpha/primary",
            ],
        )

    def test_rsync_preserves_source_path_as_one_argument(self) -> None:
        self.transport.sync_overlay(
            self.state,
            "/srv/trie-platform/workspaces/trie-space/alpha/primary",
        )
        argv, _kwargs = self.runner.calls[-1]
        self.assertEqual(
            argv,
            [
                "rsync",
                "-az",
                "--delete",
                "--safe-links",
                "--filter=merge /tmp/sync excludes.txt",
                "-e",
                "ssh",
                "/tmp/source with spaces/",
                "trie-docker:/srv/trie-platform/workspaces/trie-space/alpha/primary/",
            ],
        )


if __name__ == "__main__":
    unittest.main()
