"""Tests for isolated environment creation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
import unittest

from trie_remote.job_environment import (
    cleanup_job_builder,
    create_job_environment,
    ensure_job_builder,
)
from trie_remote.server_paths import ServerPaths


@dataclass(frozen=True)
class FixtureJob:
    job_id: str
    repository: str
    workspaces: dict[str, str]


class JobEnvironmentTests(unittest.TestCase):
    def test_creates_exact_job_environment_and_docker_shim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ServerPaths.from_root(Path(directory) / "server")
            paths.create()
            primary = Path(directory) / "center"
            process = Path(directory) / "process"
            primary.mkdir()
            process.mkdir()
            job = FixtureJob(
                job_id="alpha",
                repository="trie-center",
                workspaces={
                    "primary": str(primary),
                    "process": str(process),
                },
            )

            environment = create_job_environment(paths, job)

            self.assertEqual(environment["TRIE_REMOTE_JOB_ID"], "alpha")
            self.assertEqual(environment["COMPOSE_PROJECT_NAME"], "trie-trie-center-alpha")
            self.assertEqual(environment["BUILDX_BUILDER"], "trie-trie-center-alpha")
            self.assertEqual(
                environment["XDG_CACHE_HOME"],
                str(paths.caches / "language" / "trie-center" / "xdg"),
            )
            self.assertEqual(
                environment.get("PLAYWRIGHT_BROWSERS_PATH"),
                str(paths.caches / "playwright"),
            )
            self.assertEqual(
                environment["TRIE_M4_CENTER_NODE_DIR"],
                str(paths.toolchains / "center-node" / "bin"),
            )
            self.assertEqual(
                environment["TRIE_PROCESS_ROOT"],
                str(process),
            )
            self.assertFalse((primary / ".hermit").exists())
            shim = paths.jobs / "alpha" / "bin" / "docker"
            self.assertTrue(shim.is_file())
            self.assertTrue(shim.stat().st_mode & 0o100)
            shim_text = shim.read_text(encoding="utf-8")
            self.assertIn(str(paths.bin / "trie-runner"), shim_text)
            self.assertIn(
                'docker --job "$TRIE_REMOTE_JOB_ID" -- "$@"',
                shim_text,
            )
            pnpm_shim = paths.jobs / "alpha" / "bin" / "pnpm"
            self.assertTrue(pnpm_shim.is_file())
            self.assertIn(
                'exec corepack pnpm "$@"',
                pnpm_shim.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                environment["PATH"].split(":")[:4],
                [
                    str(shim.parent),
                    str(paths.bin),
                    str(paths.toolchains / "process-node" / "bin"),
                    str(paths.toolchains / "go" / "bin"),
                ],
            )

    def test_builder_creation_uses_exact_job_builder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ServerPaths.from_root(Path(directory) / "server")
            paths.create()
            job = FixtureJob("alpha", "trie-vms", {"primary": "/tmp/source"})
            commands: list[list[str]] = []

            def run(argv: list[str]) -> None:
                commands.append(argv)
                if "inspect" in argv:
                    raise subprocess.CalledProcessError(1, argv)

            ensure_job_builder(paths, job, run)

            self.assertEqual(
                commands,
                [
                    [
                        "/usr/bin/docker",
                        "buildx",
                        "inspect",
                        "trie-trie-vms-alpha",
                    ],
                    [
                        "/usr/bin/docker",
                        "buildx",
                        "create",
                        "--name",
                        "trie-trie-vms-alpha",
                        "--driver",
                        "docker-container",
                        "--driver-opt",
                        "default-load=true",
                        "--use",
                    ],
                ],
            )

    def test_builder_cleanup_uses_job_private_docker_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ServerPaths.from_root(Path(directory) / "server")
            paths.create()
            job = FixtureJob("alpha", "trie-vms", {"primary": "/tmp/source"})
            calls: list[tuple[list[str], dict[str, str]]] = []

            def run(argv: list[str], *, env: dict[str, str], check: bool) -> None:
                self.assertFalse(check)
                calls.append((argv, env))

            cleanup_job_builder(paths, job, run)

            self.assertEqual(
                calls[0][0],
                [
                    "/usr/bin/docker",
                    "buildx",
                    "rm",
                    "trie-trie-vms-alpha",
                ],
            )
            self.assertEqual(
                calls[0][1]["DOCKER_CONFIG"],
                str(paths.jobs / "alpha" / "docker-config"),
            )


if __name__ == "__main__":
    unittest.main()
