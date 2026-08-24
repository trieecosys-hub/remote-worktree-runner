"""Tests for published Compose port conflict detection."""

from __future__ import annotations

import unittest

from trie_remote.port_policy import (
    PortConflictError,
    compose_project_name,
    validate_published_ports,
)


class PortPolicyTests(unittest.TestCase):
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
