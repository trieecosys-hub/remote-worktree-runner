"""Tests for shared validation and configuration."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from trie_remote.common import ensure_below, validate_identifier
from trie_remote.config import RunnerConfig


class IdentifierTests(unittest.TestCase):
    def test_accepts_normalized_identifier(self) -> None:
        self.assertEqual(validate_identifier("trie-vms-42", "job"), "trie-vms-42")

    def test_rejects_path_escape_and_unsupported_characters(self) -> None:
        for value in ("../x", "/tmp/x", "x_y", "X", "", "-leading"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_identifier(value, "job")

    def test_rejects_identifier_longer_than_63_characters(self) -> None:
        with self.assertRaises(ValueError):
            validate_identifier("a" * 64, "job")


class PathContainmentTests(unittest.TestCase):
    def test_accepts_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "jobs" / "alpha"

            self.assertEqual(ensure_below(root, child), child.resolve())

    def test_rejects_sibling_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "trie"

            with self.assertRaises(ValueError):
                ensure_below(root, base / "trie-other" / "job")


class RunnerConfigTests(unittest.TestCase):
    def test_loads_safe_defaults(self) -> None:
        config = RunnerConfig.load({})

        self.assertEqual(config.ssh_alias, "trie-docker")
        self.assertEqual(config.remote_root, Path("/srv/trie-platform"))
        self.assertEqual(config.minimum_free_gib, 100)
        self.assertIn("trie-space", config.allowed_repositories)

    def test_environment_can_override_non_secret_locations(self) -> None:
        config = RunnerConfig.load(
            {
                "TRIE_REMOTE_SSH_ALIAS": "fixture-host",
                "TRIE_REMOTE_ROOT": "/tmp/trie-fixture",
            },
        )

        self.assertEqual(config.ssh_alias, "fixture-host")
        self.assertEqual(config.remote_root, Path("/tmp/trie-fixture"))

    def test_public_environment_names_take_precedence_and_parse_allowlist(self) -> None:
        config = RunnerConfig.load(
            {
                "REMOTE_RUNNER_SSH_ALIAS": "public-host",
                "TRIE_REMOTE_SSH_ALIAS": "legacy-host",
                "REMOTE_RUNNER_ROOT": "/srv/remote-worktree-runner",
                "TRIE_REMOTE_ROOT": "/srv/legacy-runner",
                "REMOTE_RUNNER_ALLOWED_REPOSITORIES": "frontend, api,frontend",
            },
        )

        self.assertEqual(config.ssh_alias, "public-host")
        self.assertEqual(
            config.remote_root,
            Path("/srv/remote-worktree-runner"),
        )
        self.assertEqual(config.allowed_repositories, frozenset({"api", "frontend"}))


if __name__ == "__main__":
    unittest.main()
