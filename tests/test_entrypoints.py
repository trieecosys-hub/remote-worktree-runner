"""Tests for the local and server command entrypoints."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def run_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the package module with the source tree on PYTHONPATH."""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "trie_remote", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


class EntrypointTests(unittest.TestCase):
    def test_local_help_identifies_trie_run(self) -> None:
        result = run_module("local", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trie-run", result.stdout)

    def test_server_help_identifies_trie_runner(self) -> None:
        result = run_module("server", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trie-runner", result.stdout)

    def test_preview_help_is_available_on_both_entrypoints(self) -> None:
        local = run_module("local", "preview", "--help")
        server = run_module("server", "preview-publish", "--help")

        self.assertEqual(local.returncode, 0, local.stderr)
        self.assertIn("{publish,list,unpublish}", local.stdout)
        self.assertEqual(server.returncode, 0, server.stderr)
        self.assertIn("--check-path", server.stdout)

    def test_missing_mode_is_a_usage_error(self) -> None:
        result = run_module()

        self.assertEqual(result.returncode, 2)
        self.assertIn("{local|server}", result.stderr)


if __name__ == "__main__":
    unittest.main()
