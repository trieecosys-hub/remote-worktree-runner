"""Tests for the local control command contract."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from trie_remote.local_cli import build_parser, main, validate_requested_command


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def ssh(self, arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout="[]\n")


class LocalCliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
