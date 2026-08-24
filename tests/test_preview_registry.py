"""Tests for transactional server-side preview publication."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from trie_remote.job_environment import job_resource_name
from trie_remote.job_store import JobSpec, JobStore
from trie_remote.preview import PreviewRoute, PreviewSlot, render_route, write_slot_configuration
from trie_remote.preview_registry import PreviewRegistry
from trie_remote.server_paths import ServerPaths


class FakeDocker:
    """Small stateful Docker boundary used by registry tests."""

    def __init__(self, edge_network: str = "remote-worktree-runner-edge") -> None:
        self.edge_network = edge_network
        self.containers: dict[str, dict[str, object]] = {}
        self.calls: list[list[str]] = []

    def add_container(
        self,
        *,
        container_id: str,
        project: str,
        service: str,
        running: bool = True,
        health: str = "healthy",
        edge_ip: str = "172.30.0.10",
    ) -> None:
        self.containers[container_id] = {
            "project": project,
            "service": service,
            "running": running,
            "health": health,
            "edge_ip": edge_ip,
            "networks": {},
        }

    def __call__(
        self,
        argv: list[str],
        *,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text
        self.calls.append(list(argv))
        self.assert_safe_inspection(argv)
        command = argv[1:]
        if command[:1] == ["ps"]:
            project = self._filter(command, "label=com.docker.compose.project=")
            service = self._filter(command, "label=com.docker.compose.service=")
            matches = [
                container_id
                for container_id, state in self.containers.items()
                if state["project"] == project and state["service"] == service
            ]
            return self.result(argv, "\n".join(matches) + ("\n" if matches else ""))
        if command[:2] == ["network", "inspect"]:
            return self.result(argv, "[]\n")
        if command[:2] == ["network", "connect"]:
            alias = command[command.index("--alias") + 1]
            network = command[-2]
            container_id = command[-1]
            state = self.containers[container_id]
            networks = state["networks"]
            assert isinstance(networks, dict)
            networks[network] = {
                "Aliases": [alias],
                "IPAddress": state["edge_ip"],
            }
            return self.result(argv)
        if command[:2] == ["network", "disconnect"]:
            network, container_id = command[-2:]
            networks = self.containers[container_id]["networks"]
            assert isinstance(networks, dict)
            networks.pop(network, None)
            return self.result(argv)
        if command[:1] == ["inspect"]:
            template = command[command.index("--format") + 1]
            container_id = command[-1]
            state = self.containers[container_id]
            if ".State.Running" in template:
                return self.result(argv, f"{str(state['running']).lower()}\n")
            if ".State.Health" in template:
                return self.result(argv, f"{state['health']}\n")
            if ".NetworkSettings.Networks" in template:
                return self.result(argv, json.dumps(state["networks"]) + "\n")
        raise AssertionError(f"unexpected Docker call: {argv}")

    @staticmethod
    def assert_safe_inspection(argv: list[str]) -> None:
        joined = " ".join(argv)
        if ".Config.Env" in joined or "{{json .Config}}" in joined:
            raise AssertionError("registry requested container environment")

    @staticmethod
    def _filter(command: list[str], prefix: str) -> str:
        for index, value in enumerate(command):
            if value == "--filter" and command[index + 1].startswith(prefix):
                return command[index + 1].removeprefix(prefix)
        raise AssertionError(f"missing filter {prefix}")

    @staticmethod
    def result(argv: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class FakeHttp:
    def __init__(self) -> None:
        self.responses: dict[str, list[int]] = {}
        self.calls: list[tuple[str, int, str, str]] = []

    def queue(self, address: str, *statuses: int) -> None:
        self.responses.setdefault(address, []).extend(statuses)

    def __call__(
        self,
        address: str,
        port: int,
        path: str,
        host_header: str,
        timeout: float = 3.0,
    ) -> int:
        del timeout
        self.calls.append((address, port, path, host_header))
        queue = self.responses.get(address, [])
        if not queue:
            raise OSError(f"no response from {address}")
        return queue.pop(0)


class PreviewRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ServerPaths.from_root(Path(self.temporary.name) / "runner")
        self.paths.create()
        self.gateway = self.paths.root / "services" / "gateway"
        self.dynamic = self.gateway / "dynamic"
        self.dynamic.mkdir(parents=True)
        (self.gateway / "gateway.env").write_text(
            "TRAEFIK_IMAGE=traefik:v3.7.11@sha256:" + "a" * 64 + "\n"
            "GATEWAY_BIND_HOST=127.0.0.1\n"
            "GATEWAY_BIND_PORT=18080\n"
            "GATEWAY_EDGE_NETWORK=remote-worktree-runner-edge\n",
            encoding="utf-8",
        )
        write_slot_configuration(
            self.gateway / "preview-slots.json",
            (
                PreviewSlot("process", "process.example.com", "example-process"),
                PreviewSlot("space", "space.example.com", "example-process"),
            ),
        )
        self.docker = FakeDocker()
        self.http = FakeHttp()

    def create_job(
        self,
        job_id: str,
        repository: str = "example-process",
        projects: tuple[str, ...] = (),
    ) -> str:
        workspace = self.paths.workspaces / repository / job_id / "primary"
        workspace.mkdir(parents=True)
        spec = JobSpec(
            job_id=job_id,
            repository=repository,
            workspace=str(workspace),
            workspaces={"primary": str(workspace)},
            includes={},
            weight="light",
            argv=("true",),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        store = JobStore(self.paths)
        store.create(spec)
        if projects:
            project_file = store.job_directory(job_id) / "compose-projects.json"
            project_file.write_text(json.dumps(list(projects)), encoding="utf-8")
        return job_resource_name(repository, job_id)

    def registry(self) -> PreviewRegistry:
        return PreviewRegistry(
            self.paths,
            run=self.docker,
            status_request=self.http,
            sleep=lambda _seconds: None,
        )

    def publish(
        self,
        *,
        job_id: str,
        project: str,
        slot: str = "process",
        service: str = "web",
        port: int = 80,
        check_path: str = "/",
    ) -> PreviewRoute:
        return self.registry().publish(
            job_id=job_id,
            slot=slot,
            project=project,
            service=service,
            port=port,
            check_path=check_path,
        )

    def test_publish_rejects_repository_mismatch(self) -> None:
        project = self.create_job("preview-01", repository="other-repository")

        with self.assertRaisesRegex(ValueError, "repository does not own slot"):
            self.publish(job_id="preview-01", project=project)

        self.assertEqual(self.docker.calls, [])

    def test_publish_rejects_unrecorded_compose_project(self) -> None:
        self.create_job("preview-01")

        with self.assertRaisesRegex(ValueError, "project is not owned by job"):
            self.publish(job_id="preview-01", project="unrelated")

        self.assertEqual(self.docker.calls, [])

    def test_publish_accepts_recorded_compose_project(self) -> None:
        self.create_job("preview-01", projects=("product-preview",))
        container_id = "a" * 64
        self.docker.add_container(
            container_id=container_id,
            project="product-preview",
            service="web",
        )
        self.http.queue("172.30.0.10", 200)
        self.http.queue("127.0.0.1", 200)

        route = self.publish(job_id="preview-01", project="product-preview")

        self.assertEqual(route.container_id, container_id)

    def test_publish_requires_exactly_one_running_healthy_container(self) -> None:
        project = self.create_job("preview-01")
        cases = (
            ((), "expected exactly one"),
            (({"container_id": "a" * 64}, {"container_id": "b" * 64}), "expected exactly one"),
            (({"container_id": "c" * 64, "running": False},), "not running"),
            (({"container_id": "d" * 64, "health": "unhealthy"},), "not healthy"),
        )
        for containers, message in cases:
            self.docker.containers.clear()
            for container in containers:
                self.docker.add_container(project=project, service="web", **container)
            with self.subTest(containers=containers), self.assertRaisesRegex(ValueError, message):
                self.publish(job_id="preview-01", project=project)

    def test_first_publish_connects_checks_and_writes_route(self) -> None:
        project = self.create_job("preview-01")
        container_id = "a" * 64
        self.docker.add_container(container_id=container_id, project=project, service="web")
        self.http.queue("172.30.0.10", 204)
        self.http.queue("127.0.0.1", 302)

        route = self.publish(job_id="preview-01", project=project, check_path="/ready")

        self.assertEqual(route.network_alias, "preview-process-aaaaaaaaaaaa")
        self.assertEqual(self.registry().list(), [route])
        self.assertEqual(
            self.http.calls,
            [
                ("172.30.0.10", 80, "/ready", "process.example.com"),
                ("127.0.0.1", 18080, "/ready", "process.example.com"),
            ],
        )
        self.assertEqual((self.dynamic / "preview-process.yaml").stat().st_mode & 0o777, 0o644)

    def test_republish_of_same_target_is_idempotent(self) -> None:
        project = self.create_job("preview-01")
        self.docker.add_container(container_id="a" * 64, project=project, service="web")
        self.http.queue("172.30.0.10", 200)
        self.http.queue("127.0.0.1", 200)
        first = self.publish(job_id="preview-01", project=project)
        calls = len(self.docker.calls)

        second = self.publish(job_id="preview-01", project=project)

        self.assertEqual(second, first)
        self.assertEqual(len(self.docker.calls), calls + 4)
        connect_calls = [call for call in self.docker.calls if call[1:3] == ["network", "connect"]]
        self.assertEqual(len(connect_calls), 1)

    def test_successful_handoff_disconnects_unused_old_container(self) -> None:
        first_project = self.create_job("preview-01")
        second_project = self.create_job("preview-02")
        first_id = "a" * 64
        second_id = "b" * 64
        self.docker.add_container(
            container_id=first_id,
            project=first_project,
            service="web",
            edge_ip="172.30.0.10",
        )
        self.docker.add_container(
            container_id=second_id,
            project=second_project,
            service="web",
            edge_ip="172.30.0.11",
        )
        self.http.queue("172.30.0.10", 200)
        self.http.queue("172.30.0.11", 200)
        self.http.queue("127.0.0.1", 200, 200)
        self.publish(job_id="preview-01", project=first_project)

        route = self.publish(job_id="preview-02", project=second_project)

        self.assertEqual(route.container_id, second_id)
        disconnects = [call for call in self.docker.calls if call[1:3] == ["network", "disconnect"]]
        self.assertEqual(disconnects[-1][-1], first_id)

    def test_handoff_keeps_old_container_referenced_by_another_slot(self) -> None:
        first_project = self.create_job("preview-01")
        second_project = self.create_job("preview-02")
        first_id = "a" * 64
        second_id = "b" * 64
        self.docker.add_container(container_id=first_id, project=first_project, service="web")
        self.docker.add_container(
            container_id=second_id,
            project=second_project,
            service="web",
            edge_ip="172.30.0.11",
        )
        other = PreviewRoute(
            slot="space",
            hostname="space.example.com",
            repository="example-process",
            job_id="preview-01",
            project=first_project,
            service="web",
            container_id=first_id,
            network_alias="preview-space-aaaaaaaaaaaa",
            port=80,
            check_path="/",
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        (self.dynamic / "preview-space.yaml").write_text(render_route(other), encoding="utf-8")
        self.http.queue("172.30.0.10", 200)
        self.http.queue("172.30.0.11", 200)
        self.http.queue("127.0.0.1", 200, 200)
        self.publish(job_id="preview-01", project=first_project)

        self.publish(job_id="preview-02", project=second_project)

        disconnects = [call for call in self.docker.calls if call[1:3] == ["network", "disconnect"]]
        self.assertFalse(any(call[-1] == first_id for call in disconnects))

    def test_direct_check_failure_leaves_route_absent_and_disconnects_candidate(self) -> None:
        project = self.create_job("preview-01")
        container_id = "a" * 64
        self.docker.add_container(container_id=container_id, project=project, service="web")
        self.http.queue("172.30.0.10", 500)

        with self.assertRaisesRegex(ValueError, "direct preview check returned HTTP 500"):
            self.publish(job_id="preview-01", project=project)

        self.assertFalse((self.dynamic / "preview-process.yaml").exists())
        self.assertNotIn(self.docker.edge_network, self.docker.containers[container_id]["networks"])

    def test_gateway_failure_restores_exact_previous_route(self) -> None:
        first_project = self.create_job("preview-01")
        second_project = self.create_job("preview-02")
        first_id = "a" * 64
        second_id = "b" * 64
        self.docker.add_container(container_id=first_id, project=first_project, service="web")
        self.docker.add_container(
            container_id=second_id,
            project=second_project,
            service="web",
            edge_ip="172.30.0.11",
        )
        self.http.queue("172.30.0.10", 200)
        self.http.queue("172.30.0.11", 200)
        self.http.queue("127.0.0.1", 200, 503, 200)
        previous = self.publish(job_id="preview-01", project=first_project)
        previous_bytes = (self.dynamic / "preview-process.yaml").read_bytes()

        with self.assertRaisesRegex(ValueError, "gateway preview check returned HTTP 503"):
            self.publish(job_id="preview-02", project=second_project)

        self.assertEqual((self.dynamic / "preview-process.yaml").read_bytes(), previous_bytes)
        self.assertEqual(self.registry().list(), [previous])
        self.assertNotIn(self.docker.edge_network, self.docker.containers[second_id]["networks"])

    def test_unpublish_requires_owner_and_disconnects_unused_container(self) -> None:
        project = self.create_job("preview-01")
        container_id = "a" * 64
        self.docker.add_container(container_id=container_id, project=project, service="web")
        self.http.queue("172.30.0.10", 200)
        self.http.queue("127.0.0.1", 200, 404)
        route = self.publish(job_id="preview-01", project=project)

        with self.assertRaisesRegex(ValueError, "preview is owned by job preview-01"):
            self.registry().unpublish("other-job", "process")
        removed = self.registry().unpublish("preview-01", "process")

        self.assertEqual(removed, route)
        self.assertEqual(self.registry().list(), [])
        self.assertNotIn(self.docker.edge_network, self.docker.containers[container_id]["networks"])

    def test_cleanup_refuses_active_owner_and_allows_other_or_absent_gateway(self) -> None:
        project = self.create_job("preview-01")
        self.create_job("preview-02")
        self.docker.add_container(container_id="a" * 64, project=project, service="web")
        self.http.queue("172.30.0.10", 200)
        self.http.queue("127.0.0.1", 200)
        self.publish(job_id="preview-01", project=project)

        with self.assertRaisesRegex(ValueError, "active previews: process"):
            self.registry().assert_cleanup_allowed("preview-01")
        self.registry().assert_cleanup_allowed("preview-02")
        (self.gateway / "preview-slots.json").unlink()
        self.registry().assert_cleanup_allowed("preview-01")

    def test_list_fails_closed_for_manually_edited_route(self) -> None:
        project = self.create_job("preview-01")
        route = PreviewRoute(
            slot="process",
            hostname="process.example.com",
            repository="example-process",
            job_id="preview-01",
            project=project,
            service="web",
            container_id="a" * 64,
            network_alias="preview-process-aaaaaaaaaaaa",
            port=80,
            check_path="/",
            published_at=datetime.now(timezone.utc).isoformat(),
        )
        content = render_route(route).replace("process.example.com", "other.example.com", 1)
        (self.dynamic / "preview-process.yaml").write_text(content, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "does not match ownership"):
            self.registry().list()


if __name__ == "__main__":
    unittest.main()
