"""Tests for the local control command contract."""

from __future__ import annotations

import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from trie_remote.job_store import OverlayManifest
from trie_remote.local_cli import (
    build_parser,
    main,
    run_worktrees,
    validate_requested_command,
)
from trie_remote.repository import RepositoryState
from trie_remote.transport import ExecutionResult, Reservation


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def ssh(
        self, arguments: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout="[]\n")


class WorkflowTransport:
    def __init__(
        self,
        *,
        fail_push: bool = False,
        interrupt_execute: bool = False,
    ) -> None:
        self.events: list[tuple[str, object]] = []
        self.fail_push = fail_push
        self.interrupt_execute = interrupt_execute

    remote_root = Path("/srv/trie-platform")
    exclude_file = Path("/tmp/sync-excludes.txt")

    def reserve(self, spec: object) -> Reservation:
        self.events.append(("reserve", spec))
        return Reservation(
            protocol_version=2,
            mirrors={"primary": "/srv/trie-platform/repos/trie-space.git"},
            workspaces={
                "primary": "/srv/trie-platform/workspaces/trie-space/alpha/primary",
            },
        )

    def push_commit(
        self,
        _state: RepositoryState,
        _job_id: str,
        role: str,
        *,
        ensure_repository: bool = True,
    ) -> None:
        self.events.append(("push", role))
        if self.fail_push:
            raise RuntimeError("push failed")

    def prepare_all(self, job_id: str) -> dict[str, str]:
        self.events.append(("prepare-all", job_id))
        return {
            "primary": "/srv/trie-platform/workspaces/trie-space/alpha/primary",
        }

    def sync_overlay(
        self,
        _state: RepositoryState,
        remote_workspace: str,
        manifest: OverlayManifest,
    ) -> None:
        if manifest.transfer:
            self.events.append(("sync", remote_workspace))

    def execute(self, job_id: str) -> ExecutionResult:
        self.events.append(("execute", job_id))
        if self.interrupt_execute:
            raise KeyboardInterrupt
        return ExecutionResult(0, {"state": "passed", "exit_code": 0})

    def ssh(
        self,
        arguments: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.events.append((arguments[0], list(arguments[1:])))
        if arguments[0] == "status":
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout='{"state":"passed","exit_code":0}\n',
            )
        return subprocess.CompletedProcess(arguments, 0, stdout="")


class LocalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = RepositoryState(
            name="trie-space",
            root=Path("/tmp/trie-space"),
            git_common_dir=Path("/tmp/trie-space/.git"),
            commit="a" * 40,
            branch="main",
            dirty=True,
        )

    def test_parses_compound_worktree_job(self) -> None:
        arguments = build_parser().parse_args(
            [
                "run",
                "--job",
                "alpha",
                "--weight",
                "heavy",
                "--include",
                "process=/tmp/trie process",
                "--",
                "bash",
                "script.sh",
            ],
        )
        self.assertEqual(arguments.job, "alpha")
        self.assertEqual(arguments.include, ["process=/tmp/trie process"])
        self.assertEqual(arguments.command_arguments, ["--", "bash", "script.sh"])
        self.assertEqual(
            build_parser()
            .parse_args(
                ["run", "--job", "exclusive", "--weight", "exclusive", "--", "true"],
            )
            .weight,
            "exclusive",
        )

    def test_has_reconnect_and_doctor_commands(self) -> None:
        parser = build_parser()
        for values in (
            ["status", "alpha"],
            ["logs", "-f", "alpha"],
            ["cancel", "alpha"],
            ["cleanup", "alpha"],
            ["doctor", "--show-sync"],
        ):
            with self.subTest(values=values):
                self.assertIsNotNone(parser.parse_args(values).command)

    def test_parses_preview_commands(self) -> None:
        parser = build_parser()
        publish = parser.parse_args(
            [
                "preview",
                "publish",
                "--job",
                "process-preview-01",
                "--slot",
                "process",
                "--project",
                "trie-process-preview",
                "--service",
                "web",
                "--port",
                "8080",
                "--check-path",
                "/health",
            ],
        )
        self.assertEqual(publish.command, "preview")
        self.assertEqual(publish.preview_command, "publish")
        self.assertEqual(publish.port, 8080)
        self.assertEqual(publish.check_path, "/health")

        listing = parser.parse_args(["preview", "list"])
        self.assertEqual(listing.preview_command, "list")

        unpublish = parser.parse_args(
            [
                "preview",
                "unpublish",
                "--job",
                "process-preview-01",
                "--slot",
                "process",
            ],
        )
        self.assertEqual(unpublish.preview_command, "unpublish")

    def test_preview_publish_forwards_exact_server_arguments(self) -> None:
        transport = FakeTransport()
        with patch("trie_remote.local_cli._transport", return_value=transport):
            result = main(
                [
                    "preview",
                    "publish",
                    "--job",
                    "process-preview-01",
                    "--slot",
                    "process",
                    "--project",
                    "trie-process-preview",
                    "--service",
                    "web",
                    "--port",
                    "8080",
                    "--check-path",
                    "/health",
                ],
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            transport.calls,
            [
                [
                    "preview-publish",
                    "--job",
                    "process-preview-01",
                    "--slot",
                    "process",
                    "--project",
                    "trie-process-preview",
                    "--service",
                    "web",
                    "--port",
                    "8080",
                    "--check-path",
                    "/health",
                ],
            ],
        )

    def test_rejects_daemon_control_commands(self) -> None:
        for argv in (
            ["systemctl", "restart", "docker"],
            ["service", "docker", "restart"],
            ["docker", "system", "prune"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                validate_requested_command(argv)

    def test_run_reserves_the_job_before_transferring_source(self) -> None:
        transport = WorkflowTransport()
        with (
            patch(
                "trie_remote.local_cli.RepositoryState.discover",
                return_value=self.state,
            ),
            patch(
                "trie_remote.local_cli.RepositoryState.overlay",
                return_value=OverlayManifest(),
            ),
            redirect_stdout(StringIO()),
        ):
            result = run_worktrees(
                self.state.root,
                {},
                "alpha",
                "light",
                ["true"],
                transport,
            )

        self.assertEqual(result, 0)
        event_names = [event[0] for event in transport.events]
        self.assertLess(event_names.index("reserve"), event_names.index("push"))
        self.assertEqual(event_names.count("reserve"), 1)
        self.assertEqual(
            event_names,
            ["reserve", "push", "prepare-all", "execute"],
        )

    def test_transfer_failure_cancels_the_preparing_job(self) -> None:
        transport = WorkflowTransport(fail_push=True)
        with (
            patch(
                "trie_remote.local_cli.RepositoryState.discover",
                return_value=self.state,
            ),
            patch(
                "trie_remote.local_cli.RepositoryState.overlay",
                return_value=OverlayManifest(),
            ),
            self.assertRaisesRegex(RuntimeError, "push failed"),
        ):
            run_worktrees(
                self.state.root,
                {},
                "alpha",
                "light",
                ["true"],
                transport,
            )

        self.assertEqual(
            [event[0] for event in transport.events],
            ["reserve", "push", "cancel"],
        )

    def test_execute_interrupt_leaves_the_remote_job_running(self) -> None:
        transport = WorkflowTransport(interrupt_execute=True)
        stderr = StringIO()
        with (
            patch(
                "trie_remote.local_cli.RepositoryState.discover",
                return_value=self.state,
            ),
            patch(
                "trie_remote.local_cli.RepositoryState.overlay",
                return_value=OverlayManifest(),
            ),
            redirect_stderr(stderr),
        ):
            result = run_worktrees(
                self.state.root,
                {},
                "alpha",
                "light",
                ["true"],
                transport,
            )

        self.assertEqual(result, 130)
        self.assertEqual(
            [event[0] for event in transport.events],
            ["reserve", "push", "prepare-all", "execute"],
        )
        self.assertIn("continues on server", stderr.getvalue())

    def test_status_and_cancel_preserve_structured_missing_job_response(self) -> None:
        class MissingTransport:
            def __init__(self) -> None:
                self.check: bool | None = None

            def ssh(
                self,
                arguments: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                self.check = bool(kwargs.get("check", True))
                return subprocess.CompletedProcess(
                    arguments,
                    3,
                    stdout='{"job_id":"missing","retryable":true,"state":"not-found"}\n',
                )

        for command in ("status", "cancel"):
            with self.subTest(command=command):
                transport = MissingTransport()
                output = StringIO()
                with (
                    patch("trie_remote.local_cli._transport", return_value=transport),
                    redirect_stdout(output),
                ):
                    result = main([command, "missing"])

                self.assertEqual(result, 3)
                self.assertFalse(transport.check)
                self.assertIn('"state":"not-found"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
