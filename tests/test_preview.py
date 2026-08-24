"""Tests for stable preview models and serialization."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from trie_remote.preview import (
    GatewayRuntime,
    PreviewRoute,
    PreviewSlot,
    load_gateway_runtime,
    load_slot_configuration,
    parse_route,
    parse_slot_spec,
    render_route,
    validate_check_path,
    validate_port,
    write_slot_configuration,
)


class PreviewSlotTests(unittest.TestCase):
    def test_parses_safe_slot_specification(self) -> None:
        slot = parse_slot_spec("process=process.example.com,example-process")

        self.assertEqual(
            slot,
            PreviewSlot(
                slot="process",
                hostname="process.example.com",
                repository="example-process",
            ),
        )

    def test_rejects_unsafe_slot_configuration(self) -> None:
        for value in (
            "Bad=app.example.com,example-app",
            "app=*.example.com,example-app",
            "app=https://app.example.com,example-app",
            "app=APP.example.com,example-app",
            "app=app.example.com,Example-App",
            "app=app.example.com",
            "app=app..example.com,example-app",
            "app=-app.example.com,example-app",
            "app=app.example.com,../example-app",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_slot_spec(value)

    def test_writes_and_loads_private_slot_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview-slots.json"
            slots = (
                parse_slot_spec("space=space.example.com,example-space"),
                parse_slot_spec("process=process.example.com,example-process"),
            )

            write_slot_configuration(path, slots)

            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(
                list(json.loads(path.read_text(encoding="utf-8"))),
                ["process", "space"],
            )
            self.assertEqual(load_slot_configuration(path)["space"], slots[0])

    def test_rejects_duplicate_slots_and_hostnames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview-slots.json"
            for slots in (
                (
                    parse_slot_spec("app=app.example.com,example-app"),
                    parse_slot_spec("app=other.example.com,example-app"),
                ),
                (
                    parse_slot_spec("app=app.example.com,example-app"),
                    parse_slot_spec("other=app.example.com,example-other"),
                ),
            ):
                with self.subTest(slots=slots), self.assertRaises(ValueError):
                    write_slot_configuration(path, slots)

    def test_rejects_unknown_or_mismatched_slot_file_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview-slots.json"
            for value in (
                {"app": {"hostname": "app.example.com", "repository": "example-app", "extra": "x"}},
                {"app": {"slot": "other", "hostname": "app.example.com", "repository": "example-app"}},
                [],
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load_slot_configuration(path)


class GatewayRuntimeTests(unittest.TestCase):
    def test_loads_only_safe_gateway_runtime_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.env"
            path.write_text(
                "TRAEFIK_IMAGE=traefik:v3.7.11@sha256:" + "a" * 64 + "\n"
                "GATEWAY_BIND_HOST=127.0.0.1\n"
                "GATEWAY_BIND_PORT=18080\n"
                "GATEWAY_EDGE_NETWORK=remote-worktree-runner-edge\n",
                encoding="utf-8",
            )

            self.assertEqual(
                load_gateway_runtime(path),
                GatewayRuntime(
                    bind_host="127.0.0.1",
                    bind_port=18080,
                    edge_network="remote-worktree-runner-edge",
                ),
            )

    def test_rejects_unsafe_or_unknown_gateway_runtime_values(self) -> None:
        safe = {
            "GATEWAY_BIND_HOST": "127.0.0.1",
            "GATEWAY_BIND_PORT": "18080",
            "GATEWAY_EDGE_NETWORK": "remote-worktree-runner-edge",
        }
        cases = (
            {**safe, "GATEWAY_BIND_HOST": "0.0.0.0"},
            {**safe, "GATEWAY_BIND_PORT": "80"},
            {**safe, "GATEWAY_EDGE_NETWORK": "Bad_Network"},
            {**safe, "PASSWORD": "do-not-read"},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.env"
            for values in cases:
                path.write_text(
                    "".join(f"{key}={value}\n" for key, value in values.items()),
                    encoding="utf-8",
                )
                with self.subTest(values=values), self.assertRaises(ValueError):
                    load_gateway_runtime(path)


class PreviewRouteTests(unittest.TestCase):
    def route(self) -> PreviewRoute:
        return PreviewRoute(
            slot="process",
            hostname="process.example.com",
            repository="example-process",
            job_id="process-preview-01",
            project="trie-example-process-preview-01",
            service="web",
            container_id="a" * 64,
            network_alias="preview-process-aaaaaaaaaaaa",
            port=80,
            check_path="/health/readiness",
            published_at="2026-08-24T04:00:00+00:00",
        )

    def test_validates_http_path_and_port(self) -> None:
        self.assertEqual(validate_check_path("/health/readiness"), "/health/readiness")
        self.assertEqual(validate_port(8080), 8080)

        for path in ("", "health", "//authority/path", "/path#fragment", "/bad\npath", "https://example.com/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_check_path(path)
        for port in (0, 65536, "8080", True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_port(port)  # type: ignore[arg-type]

    def test_round_trips_route_and_ownership_metadata(self) -> None:
        route = self.route()

        content = render_route(route)

        self.assertTrue(content.startswith("# remote-worktree-runner-preview: {"))
        self.assertIn('rule: "Host(`process.example.com`)"', content)
        self.assertIn('url: "http://preview-process-aaaaaaaaaaaa:80"', content)
        self.assertEqual(parse_route(content), route)

    def test_route_metadata_is_deterministic(self) -> None:
        first = render_route(self.route())
        second = render_route(self.route())

        self.assertEqual(first, second)

    def test_rejects_malformed_or_unknown_route_metadata(self) -> None:
        content = render_route(self.route())
        prefix, metadata = content.split("\n", 1)[0].split(": ", 1)
        value = json.loads(metadata)
        value["unknown"] = "x"
        unknown = f"{prefix}: {json.dumps(value)}\n" + content.split("\n", 1)[1]

        for candidate in ("http:\n", "# unrelated: {}\n", unknown):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                parse_route(candidate)


if __name__ == "__main__":
    unittest.main()
