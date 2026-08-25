"""Tests for worker execution, logs, and exact exit status."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
from pathlib import Path
import json
import os
import subprocess
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
    def test_doctor_requires_systemd_linger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }

            def run_command(
                command: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                output = (
                    "no\n"
                    if command[0] == "/usr/bin/loginctl"
                    else "available\n"
                )
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "trie_remote.server_cli.subprocess.run",
                    side_effect=run_command,
                ),
                redirect_stdout(output),
            ):
                result = server_main(["doctor"])

            self.assertEqual(result, 1)
            self.assertFalse(json.loads(output.getvalue())["systemd_linger"])

    def test_doctor_accepts_systemd_linger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }

            def run_command(
                command: list[str],
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                output = (
                    "yes\n"
                    if command[0] == "/usr/bin/loginctl"
                    else "available\n"
                )
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "trie_remote.server_cli.subprocess.run",
                    side_effect=run_command,
                ),
                redirect_stdout(output),
            ):
                result = server_main(["doctor"])

            self.assertEqual(result, 0)
            self.assertTrue(json.loads(output.getvalue()).get("systemd_linger"))

    def test_missing_status_and_cancel_return_structured_retryable_results(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            for command in ("status", "cancel"):
                with self.subTest(command=command):
                    output = StringIO()
                    with (
                        patch.dict(os.environ, environment, clear=True),
                        patch("trie_remote.server_cli.subprocess.run") as run,
                        redirect_stdout(output),
                    ):
                        result = server_main([command, "not-created"])

                    self.assertEqual(result, 3)
                    self.assertEqual(
                        json.loads(output.getvalue()),
                        {
                            "job_id": "not-created",
                            "retryable": True,
                            "state": "not-found",
                        },
                    )
                    run.assert_not_called()

    def test_reserve_exposes_preparing_job_and_cancel_finishes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            workspace = root / "workspaces" / "trie-space" / "reserved" / "primary"
            spec = JobSpec(
                job_id="reserved",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            reserve_output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stdin", StringIO(json.dumps(spec.to_dict()))),
                redirect_stdout(reserve_output),
            ):
                reserve_result = server_main(["reserve"])

            self.assertEqual(reserve_result, 0)
            self.assertEqual(
                json.loads(reserve_output.getvalue())["state"], "preparing"
            )
            store = JobStore(ServerPaths.from_root(root))
            self.assertEqual(store.status("reserved")["state"], "preparing")

            cancel_output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("trie_remote.server_cli.subprocess.run") as run,
                redirect_stdout(cancel_output),
            ):
                cancel_result = server_main(["cancel", "reserved"])

            self.assertEqual(cancel_result, 0)
            cancelled = json.loads(cancel_output.getvalue())
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertEqual(cancelled["exit_code"], 130)
            self.assertEqual(cancelled["reason"], "cancelled before worker start")
            run.assert_not_called()
            self.assertFalse(
                (store.job_directory("reserved") / "cancel.requested").exists()
            )

    def test_start_accepts_an_exact_reservation_and_rejects_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            workspace = root / "workspaces" / "trie-space" / "reserved" / "primary"
            spec = JobSpec(
                job_id="reserved",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
                "REMOTE_RUNNER_MINIMUM_FREE_GIB": "0",
                "REMOTE_RUNNER_WARNING_FREE_GIB": "0",
                "REMOTE_RUNNER_CANCELLATION_FREE_GIB": "0",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stdin", StringIO(json.dumps(spec.to_dict()))),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(server_main(["reserve"]), 0)

            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stdin", StringIO(json.dumps(spec.to_dict()))),
                patch("trie_remote.server_cli.subprocess.run") as run,
                redirect_stdout(output),
            ):
                result = server_main(["start"])

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "queued")
            run.assert_called_once()
            self.assertEqual(
                JobStore(ServerPaths.from_root(root)).status("reserved")["state"],
                "queued",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            workspace = root / "workspaces" / "trie-space" / "reserved" / "primary"
            spec = JobSpec(
                job_id="reserved",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            changed = JobSpec.from_dict({**spec.to_dict(), "argv": ["false"]})
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
                "REMOTE_RUNNER_MINIMUM_FREE_GIB": "0",
                "REMOTE_RUNNER_WARNING_FREE_GIB": "0",
                "REMOTE_RUNNER_CANCELLATION_FREE_GIB": "0",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stdin", StringIO(json.dumps(spec.to_dict()))),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(server_main(["reserve"]), 0)
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stdin", StringIO(json.dumps(changed.to_dict()))),
                patch("trie_remote.server_cli.subprocess.run") as run,
                self.assertRaisesRegex(ValueError, "reservation does not match"),
            ):
                server_main(["start"])
            run.assert_not_called()

    def test_start_without_reservation_remains_backward_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            workspace = root / "workspaces" / "trie-space" / "legacy" / "primary"
            spec = JobSpec(
                job_id="legacy",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
                "REMOTE_RUNNER_MINIMUM_FREE_GIB": "0",
                "REMOTE_RUNNER_WARNING_FREE_GIB": "0",
                "REMOTE_RUNNER_CANCELLATION_FREE_GIB": "0",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stdin", StringIO(json.dumps(spec.to_dict()))),
                patch("trie_remote.server_cli.subprocess.run") as run,
                redirect_stdout(output),
            ):
                result = server_main(["start"])

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["state"], "queued")
            run.assert_called_once()
            store = JobStore(ServerPaths.from_root(root))
            self.assertEqual(store.load("legacy"), spec)
            self.assertEqual(store.status("legacy")["state"], "queued")

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

    def test_worker_records_spawn_failure_without_logging_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = ServerPaths.from_root(Path(temporary) / "root")
            paths.create()
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            spec = JobSpec(
                job_id="spawn-failure",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("trie-runner-missing-executable", "private-argument-marker"),
                created_at="2026-08-24T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("spawn-failure", "queued")

            exit_code = run_job(
                paths,
                "spawn-failure",
                create_builder=False,
                disk_guard=DiskGuard(0, 0, 0, lambda _path: 1024**4),
            )

            self.assertEqual(exit_code, 127)
            status = store.status("spawn-failure")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["exit_code"], 127)
            output = store.log_path("spawn-failure").read_text()
            self.assertIn("command spawn failed", output)
            self.assertIn("FileNotFoundError", output)
            self.assertNotIn("private-argument-marker", output)

    def test_cancel_reconciles_a_collected_worker_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            paths = ServerPaths.from_root(root)
            paths.create()
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            spec = JobSpec(
                job_id="stale-job",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("stale-job", "queued", unit="trie-job-stale-job")
            store.transition("stale-job", "running")
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("trie_remote.server_cli.subprocess.run") as run,
                patch("trie_remote.server_cli.time.monotonic", side_effect=[0, 21]),
                patch("trie_remote.server_cli.time.sleep") as sleep,
                redirect_stdout(output),
            ):
                run.return_value.stdout = "LoadState=not-found\nActiveState=inactive\n"
                result = server_main(["cancel", "stale-job"])

            self.assertEqual(result, 0)
            status = store.status("stale-job")
            self.assertEqual(status["state"], "cancelled")
            self.assertEqual(status["exit_code"], 127)
            self.assertIn("worker unit is no longer active", status["reason"])
            self.assertEqual(json.loads(output.getvalue())["state"], "cancelled")
            cancellation = store.job_directory("stale-job") / "cancel.requested"
            self.assertFalse(cancellation.exists())
            sleep.assert_not_called()
            self.assertEqual(run.call_count, 1)
            self.assertIn("show", run.call_args.args[0])

    def test_cancel_reconciles_a_unit_that_stops_during_the_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            paths = ServerPaths.from_root(root)
            paths.create()
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            spec = JobSpec(
                job_id="stopping-job",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("stopping-job", "queued", unit="trie-job-stopping-job")
            store.transition("stopping-job", "running")
            active = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="LoadState=loaded\nActiveState=active\n",
            )
            inactive = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="LoadState=not-found\nActiveState=inactive\n",
            )
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "trie_remote.server_cli.subprocess.run",
                    side_effect=[active, inactive],
                ) as run,
                patch("trie_remote.server_cli.time.monotonic", side_effect=[0, 21]),
                patch("trie_remote.server_cli.time.sleep") as sleep,
                redirect_stdout(output),
            ):
                result = server_main(["cancel", "stopping-job"])

            self.assertEqual(result, 0)
            self.assertEqual(store.status("stopping-job")["state"], "cancelled")
            self.assertEqual(json.loads(output.getvalue())["exit_code"], 127)
            self.assertTrue(
                (store.job_directory("stopping-job") / "cancel.requested").exists(),
            )
            self.assertEqual(run.call_count, 2)
            sleep.assert_not_called()

    def test_cancel_keeps_the_kill_path_when_unit_state_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            paths = ServerPaths.from_root(root)
            paths.create()
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            spec = JobSpec(
                job_id="query-timeout",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("query-timeout", "queued", unit="trie-job-query-timeout")
            store.transition("query-timeout", "running")
            timeout = subprocess.TimeoutExpired("systemctl", 5)
            killed = subprocess.CompletedProcess(args=[], returncode=0)
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "trie_remote.server_cli.subprocess.run",
                    side_effect=[timeout, timeout, killed],
                ) as run,
                patch("trie_remote.server_cli.time.monotonic", side_effect=[0, 21]),
                patch("trie_remote.server_cli.time.sleep") as sleep,
                redirect_stdout(output),
            ):
                result = server_main(["cancel", "query-timeout"])

            self.assertEqual(result, 0)
            self.assertEqual(store.status("query-timeout")["state"], "running")
            self.assertEqual(
                json.loads(output.getvalue())["state"],
                "cancellation-requested",
            )
            self.assertTrue(
                (store.job_directory("query-timeout") / "cancel.requested").exists(),
            )
            self.assertEqual(run.call_count, 3)
            self.assertIn("kill", run.call_args.args[0])
            sleep.assert_not_called()

    def test_cancel_keeps_the_kill_path_for_an_active_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            paths = ServerPaths.from_root(root)
            paths.create()
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            spec = JobSpec(
                job_id="active-job",
                repository="trie-space",
                workspace=str(workspace),
                workspaces={"primary": str(workspace)},
                includes={},
                weight="light",
                argv=("true",),
                created_at="2026-08-24T00:00:00+00:00",
            )
            store = JobStore(paths)
            store.create(spec)
            store.transition("active-job", "queued", unit="trie-job-active-job")
            store.transition("active-job", "running")
            active = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="LoadState=loaded\nActiveState=activating\n",
            )
            killed = subprocess.CompletedProcess(args=[], returncode=0)
            environment = {
                "REMOTE_RUNNER_ROOT": str(root),
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "trie-space",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "trie_remote.server_cli.subprocess.run",
                    side_effect=[active, active, killed],
                ) as run,
                patch("trie_remote.server_cli.time.monotonic", side_effect=[0, 21]),
                patch("trie_remote.server_cli.time.sleep") as sleep,
                redirect_stdout(output),
            ):
                result = server_main(["cancel", "active-job"])

            self.assertEqual(result, 0)
            self.assertEqual(store.status("active-job")["state"], "running")
            self.assertEqual(
                json.loads(output.getvalue())["state"],
                "cancellation-requested",
            )
            self.assertTrue(
                (store.job_directory("active-job") / "cancel.requested").exists(),
            )
            self.assertEqual(run.call_count, 3)
            self.assertIn("kill", run.call_args.args[0])
            sleep.assert_not_called()

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
