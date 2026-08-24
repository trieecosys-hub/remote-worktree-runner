"""Static contract tests for the remote development gateway."""

from pathlib import Path
import os
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ROOT = REPOSITORY_ROOT / "gateway"
EXPECTED_IMAGE = (
    "traefik:v3.7.11@sha256:"
    "5203c3f39ca70de6790d964624e042463ffbd57715bc82be155cf224c0dd5144"
)


class GatewayAssetTests(unittest.TestCase):
    """Verify the versioned gateway assets stay hardened by default."""

    def test_versions_file_pins_immutable_traefik_image(self) -> None:
        versions = (REPOSITORY_ROOT / "config" / "versions.env").read_text()

        self.assertIn(f"TRAEFIK_IMAGE={EXPECTED_IMAGE}\n", versions)

    def test_compose_uses_loopback_default_and_hardening(self) -> None:
        compose = (GATEWAY_ROOT / "compose.yaml").read_text()

        self.assertIn("${GATEWAY_BIND_HOST:-127.0.0.1}", compose)
        self.assertIn("${GATEWAY_BIND_PORT:-18080}:8080", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("- ALL", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("/tmp:rw,noexec,nosuid,size=16m", compose)
        self.assertIn("/etc/traefik/dynamic:ro", compose)
        self.assertIn("external: true", compose)

    def test_static_configuration_uses_file_provider_and_private_health(self) -> None:
        static = (GATEWAY_ROOT / "traefik-static.yaml").read_text()

        self.assertIn('address: ":8080"', static)
        self.assertIn('address: ":8082"', static)
        self.assertIn("directory: /etc/traefik/dynamic", static)
        self.assertIn("watch: true", static)
        self.assertIn("entryPoint: health", static)
        self.assertIn("format: json", static)
        self.assertIn("defaultMode: drop", static)
        self.assertNotIn("api:", static)

    def test_example_route_is_inactive_and_uses_reserved_hostname(self) -> None:
        example = (GATEWAY_ROOT / "dynamic.example" / "route.yaml").read_text()

        self.assertIn("app.example.com", example)
        self.assertIn("http://example-service:8080", example)
        self.assertFalse((GATEWAY_ROOT / "dynamic" / "route.yaml").exists())

    def test_gateway_assets_never_mount_the_docker_socket(self) -> None:
        assets = "\n".join(
            path.read_text()
            for path in GATEWAY_ROOT.rglob("*")
            if path.is_file()
        )

        self.assertNotIn("docker.sock", assets)

    def test_live_dynamic_directory_is_ignored(self) -> None:
        gitignore = (REPOSITORY_ROOT / ".gitignore").read_text()

        self.assertIn("gateway/dynamic/", gitignore.splitlines())


class GatewayInstallerTests(unittest.TestCase):
    """Verify the gateway installer rejects unsafe deployment parameters."""

    def setUp(self) -> None:
        self.script = REPOSITORY_ROOT / "install" / "install-gateway.sh"

    def run_installer(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the installer without contacting a remote host."""
        environment = os.environ.copy()
        environment["DEPLOYMENT_PASSWORD"] = "must-not-appear"
        return subprocess.run(
            [self.script, *arguments, "--dry-run"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_dry_run_is_deterministic_and_secret_safe(self) -> None:
        arguments = (
            "--host",
            "remote-docker",
            "--remote-root",
            "/srv/remote-worktree-runner",
            "--bind-host",
            "127.0.0.1",
            "--bind-port",
            "18080",
            "--project-name",
            "remote-worktree-runner-gateway",
            "--network-name",
            "remote-worktree-runner-edge",
        )

        first = self.run_installer(*arguments)
        second = self.run_installer(*arguments)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("target: remote-docker:/srv/remote-worktree-runner", first.stdout)
        self.assertIn("endpoint: http://127.0.0.1:18080", first.stdout)
        self.assertIn(f"image: {EXPECTED_IMAGE}", first.stdout)
        self.assertNotIn("must-not-appear", first.stdout + first.stderr)

    def test_rejects_non_loopback_bind_and_invalid_port(self) -> None:
        for arguments in (
            ("--bind-host", "0.0.0.0"),
            ("--bind-port", "80"),
            ("--bind-port", "70000"),
            ("--bind-port", "not-a-port"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_installer(*arguments)
                self.assertEqual(result.returncode, 2)

    def test_rejects_shell_input_in_names_and_remote_root(self) -> None:
        for arguments in (
            ("--host", "remote;touch"),
            ("--remote-root", "/srv/../tmp"),
            ("--remote-root", "srv/runner"),
            ("--project-name", "Bad_Project"),
            ("--network-name", "edge;touch"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_installer(*arguments)
                self.assertEqual(result.returncode, 2)

    def test_installer_requires_an_immutable_image(self) -> None:
        content = self.script.read_text()

        self.assertIn("TRAEFIK_IMAGE", content)
        self.assertIn("@sha256:", content)
        self.assertIn("set -euo pipefail", content)
        self.assertNotIn("set -x", content)
        self.assertNotIn("printenv", content)

    def test_installer_uses_macos_compatible_rsync_flags(self) -> None:
        content = self.script.read_text()

        self.assertIn("rsync -a", content)
        self.assertNotIn("--chmod", content)

    def test_dry_run_validates_and_sorts_preview_slots(self) -> None:
        first = self.run_installer(
            "--preview-slot",
            "space=space.preview.example,trie-space",
            "--preview-slot",
            "process=process.preview.example,trie-process",
        )
        second = self.run_installer(
            "--preview-slot",
            "process=process.preview.example,trie-process",
            "--preview-slot",
            "space=space.preview.example,trie-space",
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_slots = [line for line in first.stdout.splitlines() if line.startswith("slot: ")]
        second_slots = [line for line in second.stdout.splitlines() if line.startswith("slot: ")]
        self.assertEqual(first_slots, second_slots)
        self.assertEqual(
            first_slots,
            [
                "slot: process -> process.preview.example (trie-process)",
                "slot: space -> space.preview.example (trie-space)",
            ],
        )

    def test_rejects_duplicate_or_invalid_preview_slots(self) -> None:
        for arguments in (
            (
                "--preview-slot",
                "process=process.preview.example,trie-process",
                "--preview-slot",
                "process=other.preview.example,trie-process",
            ),
            ("--preview-slot", "Bad=process.preview.example,trie-process"),
            ("--preview-slot", "process=*.preview.example,trie-process"),
            ("--preview-slot", "process=process.preview.example,Bad_Repo"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_installer(*arguments)
                self.assertNotEqual(result.returncode, 0)

    def test_slot_configuration_is_runtime_only(self) -> None:
        self.assertFalse((GATEWAY_ROOT / "preview-slots.json").exists())
        content = self.script.read_text()
        self.assertIn("preview-slots.json", content)
        self.assertIn("install -m 0600", content)
        self.assertIn('[[ ! -f "$install_root/preview-slots.json" ]]', content)


class GatewayVerifierTests(unittest.TestCase):
    """Verify remote checks cover gateway health, isolation, and hot reload."""

    def setUp(self) -> None:
        self.script = REPOSITORY_ROOT / "scripts" / "verify-gateway.sh"

    def test_verifier_checks_runtime_hardening(self) -> None:
        content = self.script.read_text()

        self.assertIn(".State.Health.Status", content)
        self.assertIn(".HostConfig.ReadonlyRootfs", content)
        self.assertIn(".HostConfig.CapDrop", content)
        self.assertIn(".HostConfig.RestartPolicy.Name", content)
        self.assertIn("docker.sock", content)
        self.assertIn("docker network inspect", content)
        self.assertIn("ss -H -ltn", content)
        self.assertIn("127.0.0.1", content)

    def test_verifier_checks_empty_gateway_and_dynamic_route_reload(self) -> None:
        content = self.script.read_text()

        self.assertIn("gateway-check.invalid", content)
        self.assertIn("http://127.0.0.1:8082", content)
        self.assertIn("mv ", content)
        self.assertIn("HTTP 404", content)
        self.assertIn("HTTP 200", content)
        self.assertIn("trap cleanup", content)
        self.assertIn("route_reload", content)
        self.assertNotIn("docker compose restart", content)
        self.assertNotIn("docker compose down", content)

    def test_verifier_uses_scoped_diagnostics_and_strict_validation(self) -> None:
        content = self.script.read_text()

        self.assertIn("set -euo pipefail", content)
        self.assertIn("docker compose", content)
        self.assertIn("logs --tail", content)
        self.assertNotIn("docker logs $(docker ps", content)
        self.assertNotIn("printenv", content)


class PreviewRegistryVerifierTests(unittest.TestCase):
    """Verify the disposable handoff fixture stays scoped and port-free."""

    def setUp(self) -> None:
        self.fixture = (
            REPOSITORY_ROOT
            / "tests"
            / "fixtures"
            / "preview-service"
            / "compose.yaml"
        )
        self.script = REPOSITORY_ROOT / "scripts" / "verify-preview-registry.sh"

    def test_fixture_has_ping_health_and_no_host_ports(self) -> None:
        content = self.fixture.read_text()

        self.assertIn("${TRAEFIK_IMAGE", content)
        self.assertIn(":8080", content)
        self.assertIn("--ping=true", content)
        self.assertIn("healthcheck:", content)
        self.assertNotIn("ports:", content)
        self.assertNotIn("network_mode: host", content)

    def test_verifier_proves_handoff_cleanup_guard_and_exact_cleanup(self) -> None:
        content = self.script.read_text()

        self.assertIn("set -euo pipefail", content)
        self.assertIn("preview publish", content)
        self.assertIn("preview list", content)
        self.assertIn("preview unpublish", content)
        self.assertIn('cleanup "$job_b"', content)
        self.assertIn('[[ "$container_a" != "$container_b" ]]', content)
        self.assertIn("expected cleanup refusal", content)
        self.assertNotIn("docker system prune", content)
        self.assertNotIn("docker volume prune", content)
        self.assertNotIn("docker network prune", content)


if __name__ == "__main__":
    unittest.main()
