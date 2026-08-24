"""Static contract tests for the remote development gateway."""

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
