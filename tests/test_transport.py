"""Tests for exact SSH, Git, and rsync command construction."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from trie_remote.job_store import JobSpec, OverlayManifest
from trie_remote.repository import RepositoryState
from trie_remote.transport import Reservation, Transport


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        stdout = (
            '{"workspace":"/srv/trie-platform/workspaces/trie-space/alpha/primary"}\n'
        )
        if "reserve" in argv:
            stdout = (
                '{"protocol_version":2,"job_id":"alpha","state":"preparing",'
                '"mirrors":{"primary":"/srv/trie-platform/repos/trie-space.git"},'
                '"workspaces":{"primary":"/srv/trie-platform/workspaces/'
                'trie-space/alpha/primary"}}\n'
            )
        elif "prepare-all" in argv:
            stdout = (
                '{"job_id":"alpha","workspaces":{"primary":'
                '"/srv/trie-platform/workspaces/trie-space/alpha/primary"}}\n'
            )
        elif "execute" in argv:
            stdout = '{"state":"passed","exit_code":0}\n'
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class TransportTests(unittest.TestCase):
    def test_sync_policy_excludes_nested_build_caches(self) -> None:
        policy = (
            Path(__file__).resolve().parents[1] / "config/sync-excludes.txt"
        ).read_text()
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
        self.transport.push_commit(
            self.state,
            "alpha",
            "primary",
            ensure_repository=False,
        )
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

    def test_reserve_and_prepare_all_use_one_server_call_each(self) -> None:
        workspace = "/srv/trie-platform/workspaces/trie-space/alpha/primary"
        spec = JobSpec(
            job_id="alpha",
            repository="trie-space",
            workspace=workspace,
            workspaces={"primary": workspace},
            includes={},
            weight="light",
            argv=("true",),
            created_at="2026-08-25T00:00:00+00:00",
            commits={"primary": "a" * 40},
            overlays={"primary": OverlayManifest()},
        )

        reservation = self.transport.reserve(spec)
        prepared = self.transport.prepare_all("alpha")

        self.assertEqual(
            reservation,
            Reservation(
                protocol_version=2,
                mirrors={"primary": "/srv/trie-platform/repos/trie-space.git"},
                workspaces={"primary": workspace},
            ),
        )
        self.assertEqual(prepared, {"primary": workspace})
        ssh_calls = [call for call in self.runner.calls if call[0][0] == "ssh"]
        self.assertEqual(len(ssh_calls), 2)

    def test_workspace_path_uses_the_server_contract(self) -> None:
        workspace = self.transport.workspace_path(self.state, "alpha", "primary")

        argv, _kwargs = self.runner.calls[-1]
        self.assertEqual(
            argv,
            [
                "ssh",
                "trie-docker",
                "/srv/trie-platform/bin/trie-runner",
                "workspace-path",
                "--job",
                "alpha",
                "--repository",
                "trie-space",
                "--role",
                "primary",
            ],
        )
        self.assertEqual(
            workspace,
            "/srv/trie-platform/workspaces/trie-space/alpha/primary",
        )

    def test_rsync_preserves_source_path_as_one_argument(self) -> None:
        self.transport.sync_overlay(
            self.state,
            "/srv/trie-platform/workspaces/trie-space/alpha/primary",
            OverlayManifest(transfer=("src/file name.py", "web/app.ts")),
        )
        argv, _kwargs = self.runner.calls[-1]
        self.assertEqual(
            argv,
            [
                "rsync",
                "-az",
                "--from0",
                "--files-from=-",
                "--relative",
                "--safe-links",
                "--filter=merge /tmp/sync excludes.txt",
                "-e",
                "ssh",
                "/tmp/source with spaces/",
                "trie-docker:/srv/trie-platform/workspaces/trie-space/alpha/primary/",
            ],
        )
        self.assertEqual(
            self.runner.calls[-1][1]["input"],
            b"src/file name.py\0web/app.ts\0",
        )

    def test_clean_overlay_skips_rsync(self) -> None:
        before = len(self.runner.calls)

        self.transport.sync_overlay(
            self.state,
            "/srv/trie-platform/workspaces/trie-space/alpha/primary",
            OverlayManifest(),
        )

        self.assertEqual(len(self.runner.calls), before)

    def test_execute_uses_one_ssh_session_and_returns_final_status(self) -> None:
        result = self.transport.execute("alpha")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.status, {"state": "passed", "exit_code": 0})
        self.assertEqual(self.runner.calls[-1][0][-3:], ["execute", "--job", "alpha"])


if __name__ == "__main__":
    unittest.main()
