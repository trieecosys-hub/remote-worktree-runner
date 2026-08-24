"""Tests for worker execution, logs, and exact exit status."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from trie_remote.job_store import JobSpec, JobStore
from trie_remote.job_worker import run_job
from trie_remote.scheduler import DiskGuard
from trie_remote.server_paths import ServerPaths


class JobLifecycleTests(unittest.TestCase):
    def test_worker_persists_output_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ServerPaths.from_root(Path(temporary) / "root")
            paths.create()
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            spec = JobSpec(
                job_id="fixture",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("python3", "-c", "print('marker'); raise SystemExit(7)"),
                created_at="2026-08-23T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("fixture", "queued")
            exit_code = run_job(
                paths,
                "fixture",
                create_builder=False,
                disk_guard=DiskGuard(0, 0, 0, lambda _path: 1024**4),
            )
            self.assertEqual(exit_code, 7)
            status = store.status("fixture")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["exit_code"], 7)
            self.assertIn("marker", store.log_path("fixture").read_text())


if __name__ == "__main__":
    unittest.main()
