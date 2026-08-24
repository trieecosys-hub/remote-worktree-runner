"""Tests for the local control command contract."""

from __future__ import annotations

import unittest

from trie_remote.local_cli import build_parser, validate_requested_command


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
