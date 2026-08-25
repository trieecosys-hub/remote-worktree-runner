"""Tests for published Compose port conflict detection."""

from __future__ import annotations

import subprocess
import unittest

from trie_remote.port_policy import (
    PortConflictError,
    compose_project_name,
    validate_compose_command,
    validate_published_ports,
)


class PortPolicyTests(unittest.TestCase):
    def test_compose_config_failure_includes_stable_stderr_without_traceback(
        self,
    ) -> None:
        def fail(
            command: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(
                1,
                command,
                stderr="required variable TRIE_CENTER_ROOT is missing",
            )

        with self.assertRaisesRegex(
            PortConflictError,
            "Compose configuration failed.*TRIE_CENTER_ROOT",
        ):
            validate_compose_command(
                ["compose", "up", "--detach"],
                "trie-space-fixture",
                fail,
            )

    def test_uses_product_owned_explicit_compose_project(self) -> None:
        self.assertEqual(
            compose_project_name(
                ["compose", "--project-name", "triespace-onprem-a1b2", "up"],
                "trie-trie-space-pilot",
            ),
            "triespace-onprem-a1b2",
        )

    def test_rejects_port_owned_by_another_project(self) -> None:
        compose = {
            "services": {
                "web": {
                    "ports": [
                        {"published": "8080", "target": 80, "protocol": "tcp"},
                    ],
                },
            },
        }
        running = {(8080, "tcp"): "other-project"}

        with self.assertRaises(PortConflictError):
            validate_published_ports(compose, running, "trie-vms-alpha")

    def test_allows_port_owned_by_same_project(self) -> None:
        compose = {
            "services": {
                "web": {
                    "ports": [
                        {"published": 8080, "target": 80, "protocol": "tcp"},
                    ],
                },
            },
        }
        running = {(8080, "tcp"): "trie-vms-alpha"}

        validate_published_ports(compose, running, "trie-vms-alpha")

    def test_ignores_services_without_published_ports(self) -> None:
        compose = {"services": {"database": {"expose": [5432]}}}

        validate_published_ports(compose, {}, "trie-vms-alpha")


if __name__ == "__main__":
    unittest.main()
