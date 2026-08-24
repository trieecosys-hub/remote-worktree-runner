"""Tests for worker execution, logs, and exact exit status."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from trie_remote.job_store import JobSpec, JobStore
from trie_remote.job_worker import run_job
from trie_remote.preview import PreviewRoute
from trie_remote.scheduler import DiskGuard
from trie_remote.server_cli import main as server_main
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

    def test_cleanup_checks_preview_ownership_before_docker_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            paths = ServerPaths.from_root(root)
            paths.create()
            workspace = paths.workspaces / "trie-space" / "fixture" / "primary"
            workspace.mkdir(parents=True)
            spec = JobSpec(
                job_id="fixture",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-23T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("fixture", "queued")
            store.transition("fixture", "running")
            store.transition("fixture", "passed", exit_code=0)
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "trie_remote.server_cli.PreviewRegistry.assert_cleanup_allowed",
                    side_effect=ValueError("job owns active previews: space"),
                ) as guard,
                patch("trie_remote.server_cli.subprocess.run") as run,
                self.assertRaisesRegex(ValueError, "active previews"),
            ):
                server_main(["cleanup", "fixture"])

            guard.assert_called_once_with("fixture")
            run.assert_not_called()

    def test_server_preview_publish_returns_machine_readable_route(self) -> None:
        route = PreviewRoute(
            slot="space",
            hostname="space.preview.example",
            repository="trie-space",
            job_id="space-preview-01",
            project="trie-space-preview",
            service="web",
            container_id="a" * 64,
            network_alias="preview-space-aaaaaaaaaaaa",
            port=8080,
            check_path="/health",
            published_at="2026-08-24T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "REMOTE_RUNNER_ROOT": str(Path(temporary) / "root"),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("trie_remote.server_cli.PreviewRegistry") as registry_class,
                redirect_stdout(output),
            ):
                registry_class.return_value.publish.return_value = route
                result = server_main(
                    [
                        "preview-publish",
                        "--job",
                        "space-preview-01",
                        "--slot",
                        "space",
                        "--project",
                        "trie-space-preview",
                        "--service",
                        "web",
                        "--port",
                        "8080",
                        "--check-path",
                        "/health",
                    ],
                )

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), asdict(route))
            registry_class.return_value.publish.assert_called_once_with(
                job_id="space-preview-01",
                slot="space",
                project="trie-space-preview",
                service="web",
                port=8080,
                check_path="/health",
            )


if __name__ == "__main__":
    unittest.main()
