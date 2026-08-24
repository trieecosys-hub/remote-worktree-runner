"""Tests for job-scoped Docker command policy."""

from __future__ import annotations

import unittest

from trie_remote.docker_policy import PolicyError, validate_docker_arguments


class DockerPolicyTests(unittest.TestCase):
    def test_rejects_daemon_wide_operations(self) -> None:
        blocked = (
            ["system", "prune"],
            ["volume", "prune"],
            ["image", "prune", "-a"],
            ["builder", "prune"],
            ["context", "rm", "prod"],
            ["swarm", "leave"],
        )

        for arguments in blocked:
            with self.subTest(arguments=arguments), self.assertRaises(PolicyError):
                validate_docker_arguments(arguments, "trie-vms-alpha")

    def test_allows_normal_job_commands_without_rewriting(self) -> None:
        allowed = (
            ["version"],
            ["compose", "up", "-d"],
            ["buildx", "build", "."],
        )

        for arguments in allowed:
            with self.subTest(arguments=arguments):
                self.assertIsNone(
                    validate_docker_arguments(arguments, "trie-vms-alpha"),
                )

    def test_buildx_prune_requires_exact_job_builder(self) -> None:
        validate_docker_arguments(
            ["buildx", "prune", "--builder", "trie-vms-alpha", "--force"],
            "trie-vms-alpha",
        )

        with self.assertRaises(PolicyError):
            validate_docker_arguments(
                ["buildx", "prune", "--builder", "trie-vms-beta", "--force"],
                "trie-vms-alpha",
            )

    def test_global_docker_flags_do_not_hide_blocked_command(self) -> None:
        with self.assertRaises(PolicyError):
            validate_docker_arguments(
                ["--log-level", "debug", "system", "prune"],
                "trie-vms-alpha",
            )


if __name__ == "__main__":
    unittest.main()

