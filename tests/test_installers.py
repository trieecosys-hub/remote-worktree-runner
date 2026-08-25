"""Contract tests for reproducible and non-destructive installers."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_linger_helper_prefers_user_authorization_over_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            state = temporary_path / "linger-state"
            state.write_text("no\n", encoding="utf-8")
            sudo_marker = temporary_path / "sudo-called"
            commands = {
                "id": """#!/usr/bin/env bash
if [[ "$1" == "-un" ]]; then echo runner; else echo 1000; fi
""",
                "loginctl": """#!/usr/bin/env bash
if [[ "$1" == "show-user" ]]; then cat "$LINGER_STATE"; exit 0; fi
if [[ "$1" == "enable-linger" ]]; then printf 'yes\\n' >"$LINGER_STATE"; exit 0; fi
exit 2
""",
                "sudo": """#!/usr/bin/env bash
touch "$SUDO_MARKER"
exit 1
""",
            }
            for name, content in commands.items():
                command = bin_path / name
                command.write_text(content, encoding="utf-8")
                command.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{bin_path}:{os.environ['PATH']}",
                "LINGER_STATE": str(state),
                "SUDO_MARKER": str(sudo_marker),
            }

            result = subprocess.run(
                ["bash", ROOT / "install" / "ensure-systemd-linger.sh"],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state.read_text(encoding="utf-8"), "yes\n")
            self.assertFalse(sudo_marker.exists())

    def test_zipapp_build_is_byte_reproducible(self) -> None:
        script = ROOT / "install" / "build-zipapp.sh"
        output = ROOT / "dist" / "trie-remote.pyz"

        subprocess.run([script], cwd=ROOT, check=True, capture_output=True)
        first = output.read_bytes()
        time.sleep(2.1)
        subprocess.run([script], cwd=ROOT, check=True, capture_output=True)

        self.assertEqual(output.read_bytes(), first)

    def test_shell_installers_are_strict_and_support_dry_run(self) -> None:
        for relative in (
            "install/build-zipapp.sh",
            "install/install-local.sh",
            "install/install-server.sh",
        ):
            with self.subTest(relative=relative):
                content = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("set -euo pipefail", content)
                self.assertIn("--dry-run", content)

    def test_server_installer_pins_and_verifies_downloads(self) -> None:
        content = (ROOT / "install/install-server.sh").read_text(encoding="utf-8")
        self.assertIn("sha256", content.lower())
        self.assertIn("KUBECTL_VERSION", content)
        self.assertIn("KIND_VERSION", content)
        self.assertIn("NODE_PROCESS_VERSION", content)
        self.assertIn("NODE_CENTER_VERSION", content)
        self.assertIn("GO_LINUX_AMD64_SHA256", content)
        versions = (ROOT / "config/versions.env").read_text(encoding="utf-8")
        self.assertIn("KUBECTL_VERSION=v1.36.4", versions)
        self.assertIn("KIND_VERSION=v0.32.0", versions)
        self.assertIn("JQ_VERSION=1.8.2", versions)
        self.assertIn("RIPGREP_VERSION=15.2.0", versions)
        self.assertIn("NODE_PROCESS_VERSION=v22.22.2", versions)
        self.assertIn("NODE_CENTER_VERSION=v24.19.0", versions)
        self.assertIn("GO_VERSION=1.25.13", versions)

    def test_server_installer_dry_run_includes_ripgrep(self) -> None:
        result = subprocess.run(
            [ROOT / "install" / "install-server.sh", "--dry-run"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("checksum-verified ripgrep 15.2.0", result.stdout)

    def test_server_installer_dry_run_includes_systemd_linger(self) -> None:
        result = subprocess.run(
            [ROOT / "install" / "install-server.sh", "--dry-run"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn(
            "would enable and verify systemd lingering for the remote runner account",
            result.stdout,
        )

    def test_server_installer_configures_a_positive_heavy_job_limit(self) -> None:
        script = ROOT / "install" / "install-server.sh"
        accepted = subprocess.run(
            [script, "--max-heavy-jobs", "3", "--dry-run"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        zero = subprocess.run(
            [script, "--max-heavy-jobs", "0", "--dry-run"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        invalid = subprocess.run(
            [script, "--max-heavy-jobs", "three", "--dry-run"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertIn("would configure 3 concurrent heavy jobs", accepted.stdout)
        self.assertEqual(zero.returncode, 2)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("invalid max heavy jobs", zero.stderr)
        self.assertIn("invalid max heavy jobs", invalid.stderr)
        installer = script.read_text(encoding="utf-8")
        self.assertIn("REMOTE_RUNNER_MAX_HEAVY_JOBS", installer)
        self.assertIn("$max_heavy_jobs", installer)

    def test_server_installer_has_a_runner_only_hot_update(self) -> None:
        result = subprocess.run(
            [
                ROOT / "install" / "install-server.sh",
                "--runner-only",
                "--max-heavy-jobs",
                "3",
                "--dry-run",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn(
            "would install only the runner artifact and wrapper", result.stdout
        )
        self.assertIn("would configure 3 concurrent heavy jobs", result.stdout)
        self.assertNotIn("would download", result.stdout)

    def test_server_installer_accepts_safe_remote_root_and_rejects_shell_input(
        self,
    ) -> None:
        script = ROOT / "install" / "install-server.sh"
        safe = subprocess.run(
            [
                script,
                "--host",
                "remote-docker",
                "--remote-root",
                "/srv/remote-worktree-runner",
                "--repositories",
                "remote-worktree-runner,example-api",
                "--dry-run",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        unsafe = subprocess.run(
            [script, "--remote-root", "/srv/runner;touch", "--dry-run"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertIn(
            "remote-docker:/srv/remote-worktree-runner/bin",
            safe.stdout,
        )
        self.assertEqual(unsafe.returncode, 2)
        self.assertIn("invalid remote root", unsafe.stderr)

        content = script.read_text(encoding="utf-8")
        self.assertIn("REMOTE_RUNNER_ROOT", content)
        self.assertIn("REMOTE_RUNNER_ALLOWED_REPOSITORIES", content)

    def test_deployment_verifier_uses_scoped_scheduler_fixture_jobs(self) -> None:
        content = (ROOT / "scripts" / "verify-deployment.sh").read_text(
            encoding="utf-8",
        )

        self.assertIn("set -euo pipefail", content)
        self.assertIn("--weight heavy", content)
        self.assertIn("--weight exclusive", content)
        self.assertIn('cleanup "$fixture_job" --volumes', content)
        self.assertIn("heavy jobs did not overlap", content)
        self.assertIn("exclusive job bypassed heavy jobs", content)
        self.assertNotIn("docker system prune", content)
        self.assertNotIn("docker volume prune", content)

    def test_public_project_files_and_ci_contract(self) -> None:
        for relative in (
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "docs/architecture.md",
            "docs/security-model.md",
            ".github/pull_request_template.md",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())

        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8",
        )
        self.assertIn('python-version: ["3.10", "3.14"]', workflow)
        self.assertIn("python3 -u -m unittest discover -s tests -v", workflow)
        self.assertIn("bash -n install/*.sh bin/* scripts/*.sh", workflow)
        self.assertIn("install/build-zipapp.sh", workflow)

    def test_gateway_public_documentation_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs" / "architecture.md").read_text(
            encoding="utf-8",
        )
        security = (ROOT / "docs" / "security-model.md").read_text(
            encoding="utf-8",
        )

        self.assertIn("install/install-gateway.sh", readme)
        self.assertIn("scripts/verify-gateway.sh", readme)
        self.assertIn("127.0.0.1:18080", readme)
        self.assertIn("healthy gateway starts with no product routes", readme)
        self.assertIn("/srv/remote-worktree-runner/services/gateway", readme)

        self.assertIn("Traefik development gateway", architecture)
        self.assertIn("file provider", architecture)
        self.assertIn("remote-worktree-runner-edge", architecture)
        self.assertIn("gateway/dynamic", architecture)

        self.assertIn("does not mount the Docker socket", security)
        self.assertIn("loopback", security)
        self.assertIn("Cloudflare Tunnel", security)
        self.assertIn("databases", security)

    def test_preview_registry_public_documentation_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs" / "architecture.md").read_text(
            encoding="utf-8",
        )
        security = (ROOT / "docs" / "security-model.md").read_text(
            encoding="utf-8",
        )

        for command in (
            "trie-run preview publish",
            "trie-run preview list",
            "trie-run preview unpublish",
        ):
            self.assertIn(command, readme)
        self.assertIn("--preview-slot", readme)
        self.assertIn("stable handoff", readme)
        self.assertIn("cleanup refuses", readme)
        self.assertIn("scripts/verify-preview-registry.sh", readme)

        self.assertIn("ownership record", architecture)
        self.assertIn("unique network alias", architecture)
        self.assertIn("rollback", architecture)

        self.assertIn("stateful", security)
        self.assertIn("shared edge network", security)
        self.assertIn("active preview", security)


if __name__ == "__main__":
    unittest.main()
