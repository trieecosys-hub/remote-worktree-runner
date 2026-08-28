"""Transactional publication of job-owned services through the gateway."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from trie_remote.common import validate_identifier
from trie_remote.job_environment import job_resource_name
from trie_remote.job_store import JobStore
from trie_remote.preview import (
    GatewayRuntime,
    PreviewRoute,
    PreviewSlot,
    load_gateway_runtime,
    load_slot_configuration,
    parse_route,
    render_route,
    validate_check_path,
    validate_port,
)
from trie_remote.server_paths import ServerPaths


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
StatusRequest = Callable[[str, int, str, str, float], int]
Sleep = Callable[[float], None]


def http_status(
    address: str,
    port: int,
    path: str,
    host_header: str,
    timeout: float = 3.0,
) -> int:
    """Return one HTTP response status without following redirects."""
    connection = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        connection.request(
            "GET",
            path,
            headers={"Host": host_header, "Connection": "close"},
        )
        response = connection.getresponse()
        response.read(64 * 1024)
        return int(response.status)
    finally:
        connection.close()


class PreviewRegistry:
    """Own stable preview handoff for one remote runner installation."""

    def __init__(
        self,
        paths: ServerPaths,
        *,
        run: RunCommand = subprocess.run,
        status_request: StatusRequest = http_status,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.paths = paths
        self.run = run
        self.status_request = status_request
        self.sleep = sleep
        self.gateway = paths.root / "services" / "gateway"
        self.dynamic = self.gateway / "dynamic"
        self.slot_file = self.gateway / "preview-slots.json"
        self.runtime_file = self.gateway / "gateway.env"
        self.lock_file = self.gateway / "preview.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if not self.gateway.is_dir() or not self.dynamic.is_dir():
            raise ValueError("preview gateway is not installed")
        descriptor = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(self.lock_file, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _slots(self) -> dict[str, PreviewSlot]:
        return load_slot_configuration(self.slot_file)

    def _runtime(self) -> GatewayRuntime:
        return load_gateway_runtime(self.runtime_file)

    def _route_path(self, slot: str) -> Path:
        safe = validate_identifier(slot, "preview slot")
        path = self.dynamic / f"preview-{safe}.yaml"
        if path.is_symlink():
            raise ValueError("preview route must not be a symlink")
        return path

    def _load_routes(self) -> list[PreviewRoute]:
        slots = self._slots()
        routes: list[PreviewRoute] = []
        for path in sorted(self.dynamic.glob("preview-*.yaml")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("invalid preview route path")
            route = parse_route(path.read_text(encoding="utf-8"))
            if path != self._route_path(route.slot):
                raise ValueError("preview route filename does not match ownership")
            configured = slots.get(route.slot)
            if configured is None or (
                route.hostname != configured.hostname
                or route.repository != configured.repository
            ):
                raise ValueError("preview route does not match slot configuration")
            routes.append(route)
        return sorted(routes, key=lambda route: route.slot)

    def list(self) -> list[PreviewRoute]:
        """Return active routes after fail-closed ownership validation."""
        with self._lock():
            return self._load_routes()

    def _run_docker(self, arguments: list[str]) -> str:
        result = self.run(
            ["/usr/bin/docker", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return str(result.stdout)

    def _allowed_projects(self, job_id: str, repository: str) -> set[str]:
        projects = {job_resource_name(repository, job_id)}
        path = self.paths.jobs / job_id / "compose-projects.json"
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError("invalid recorded Compose projects") from error
            if not isinstance(raw, list):
                raise ValueError("invalid recorded Compose projects")
            for value in raw:
                if not isinstance(value, str):
                    raise ValueError("invalid recorded Compose project")
                projects.add(validate_identifier(value, "Compose project"))
        return projects

    def _resolve_container(self, project: str, service: str) -> str:
        output = self._run_docker(
            [
                "ps",
                "--all",
                "--no-trunc",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ],
        )
        matches = [line.strip() for line in output.splitlines() if line.strip()]
        if len(matches) != 1:
            raise ValueError("expected exactly one preview service container")
        container_id = matches[0]
        if len(container_id) != 64 or any(character not in "0123456789abcdef" for character in container_id):
            raise ValueError("Docker returned an invalid container ID")
        running = self._run_docker(
            ["inspect", "--format", "{{.State.Running}}", container_id],
        ).strip()
        if running != "true":
            raise ValueError("preview service container is not running")
        health = self._run_docker(
            [
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container_id,
            ],
        ).strip()
        if health not in {"none", "healthy"}:
            raise ValueError("preview service container is not healthy")
        return container_id

    def _container_networks(self, container_id: str) -> dict[str, object]:
        output = self._run_docker(
            [
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                container_id,
            ],
        )
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise ValueError("invalid container network inspection") from error
        if not isinstance(value, dict):
            raise ValueError("invalid container network inspection")
        return value

    def _connect_candidate(
        self,
        runtime: GatewayRuntime,
        slot: str,
        container_id: str,
    ) -> tuple[str, str, bool]:
        alias = f"preview-{slot}-{container_id[:12]}"
        networks = self._container_networks(container_id)
        current = networks.get(runtime.edge_network)
        if current is not None:
            if not isinstance(current, dict):
                raise ValueError("invalid gateway network attachment")
            aliases = current.get("Aliases") or []
            if alias not in aliases:
                reusable = [
                    value
                    for value in aliases
                    if isinstance(value, str) and value.startswith("preview-")
                ]
                if not reusable:
                    raise ValueError("container is attached without a preview alias")
                alias = sorted(reusable)[0]
            address = current.get("IPAddress")
            if not isinstance(address, str) or not address:
                raise ValueError("preview container has no edge-network address")
            return alias, address, False

        self._run_docker(["network", "inspect", runtime.edge_network])
        self._run_docker(
            [
                "network",
                "connect",
                "--alias",
                alias,
                runtime.edge_network,
                container_id,
            ],
        )
        attached = self._container_networks(container_id).get(runtime.edge_network)
        if not isinstance(attached, dict):
            raise ValueError("failed to attach preview container to gateway network")
        address = attached.get("IPAddress")
        if not isinstance(address, str) or not address:
            raise ValueError("preview container has no edge-network address")
        return alias, address, True

    def _status(self, address: str, port: int, route: PreviewRoute) -> int:
        return self.status_request(
            address,
            port,
            route.check_path,
            route.hostname,
            3.0,
        )

    @staticmethod
    def _successful(status: int) -> bool:
        return 200 <= status <= 399

    def _gateway_status(self, runtime: GatewayRuntime, route: PreviewRoute) -> int:
        last_status = 0
        self.sleep(0.25)
        for _attempt in range(20):
            try:
                last_status = self._status(runtime.bind_host, runtime.bind_port, route)
            except OSError:
                self.sleep(0.25)
                continue
            if self._successful(last_status) or last_status >= 500:
                return last_status
            self.sleep(0.25)
        return last_status

    def _wait_for_status(
        self,
        runtime: GatewayRuntime,
        route: PreviewRoute,
        expected: Callable[[int], bool],
    ) -> int:
        last_status = 0
        for _attempt in range(20):
            try:
                last_status = self._status(runtime.bind_host, runtime.bind_port, route)
            except OSError:
                self.sleep(0.25)
                continue
            if expected(last_status):
                return last_status
            self.sleep(0.25)
        return last_status

    def _write_route(self, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        descriptor, temporary = tempfile.mkstemp(dir=self.dynamic, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _disconnect_if_unused(
        self,
        runtime: GatewayRuntime,
        container_id: str,
        *,
        routes: list[PreviewRoute] | None = None,
    ) -> None:
        active = routes if routes is not None else self._load_routes()
        if any(route.container_id == container_id for route in active):
            return
        self._run_docker(
            ["network", "disconnect", runtime.edge_network, container_id],
        )

    def publish(
        self,
        *,
        job_id: str,
        slot: str,
        project: str,
        service: str,
        port: int,
        check_path: str,
    ) -> PreviewRoute:
        """Atomically hand one configured slot to a verified job service."""
        safe_job = validate_identifier(job_id, "job")
        safe_slot = validate_identifier(slot, "preview slot")
        safe_project = validate_identifier(project, "Compose project")
        safe_service = validate_identifier(service, "Compose service")
        safe_port = validate_port(port)
        safe_path = validate_check_path(check_path)
        with self._lock():
            slots = self._slots()
            configured = slots.get(safe_slot)
            if configured is None:
                raise ValueError(f"unknown preview slot: {safe_slot}")
            store = JobStore(self.paths)
            spec = store.load(safe_job)
            reserved_repositories = {spec.repository, *spec.includes.values()}
            if configured.repository not in reserved_repositories:
                raise ValueError("job repository does not own slot")
            if safe_project not in self._allowed_projects(safe_job, spec.repository):
                raise ValueError("Compose project is not owned by job")
            container_id = self._resolve_container(safe_project, safe_service)
            route_path = self._route_path(safe_slot)
            previous_bytes = route_path.read_bytes() if route_path.is_file() else None
            previous = parse_route(previous_bytes.decode()) if previous_bytes is not None else None
            runtime = self._runtime()
            alias, address, connected_now = self._connect_candidate(
                runtime,
                safe_slot,
                container_id,
            )
            candidate = PreviewRoute(
                slot=safe_slot,
                hostname=configured.hostname,
                repository=configured.repository,
                job_id=safe_job,
                project=safe_project,
                service=safe_service,
                container_id=container_id,
                network_alias=alias,
                port=safe_port,
                check_path=safe_path,
                published_at=datetime.now(timezone.utc).isoformat(),
            )
            if previous is not None and all(
                getattr(previous, field) == getattr(candidate, field)
                for field in (
                    "slot",
                    "hostname",
                    "repository",
                    "job_id",
                    "project",
                    "service",
                    "container_id",
                    "network_alias",
                    "port",
                    "check_path",
                )
            ):
                return previous
            try:
                direct = self._status(address, safe_port, candidate)
                if not self._successful(direct):
                    raise ValueError(f"direct preview check returned HTTP {direct}")
                self._write_route(route_path, render_route(candidate).encode())
                gateway = self._gateway_status(runtime, candidate)
                if not self._successful(gateway):
                    raise ValueError(f"gateway preview check returned HTTP {gateway}")
            except (OSError, ValueError):
                self._write_route(route_path, previous_bytes)
                if previous is not None:
                    self._wait_for_status(runtime, previous, self._successful)
                if connected_now:
                    self._disconnect_if_unused(runtime, container_id)
                raise
            if previous is not None and previous.container_id != container_id:
                self._disconnect_if_unused(runtime, previous.container_id)
            return candidate

    def unpublish(self, job_id: str, slot: str) -> PreviewRoute:
        """Remove one route only when the caller names its current owner."""
        safe_job = validate_identifier(job_id, "job")
        safe_slot = validate_identifier(slot, "preview slot")
        with self._lock():
            route_path = self._route_path(safe_slot)
            if not route_path.is_file():
                raise ValueError(f"preview is not published: {safe_slot}")
            route = parse_route(route_path.read_text(encoding="utf-8"))
            if route.job_id != safe_job:
                raise ValueError(f"preview is owned by job {route.job_id}")
            runtime = self._runtime()
            self._write_route(route_path, None)
            status = self._wait_for_status(runtime, route, lambda value: value == 404)
            if status != 404:
                self._write_route(route_path, render_route(route).encode())
                raise ValueError(f"gateway preview remained active with HTTP {status}")
            self._disconnect_if_unused(runtime, route.container_id)
            return route

    def assert_cleanup_allowed(self, job_id: str) -> None:
        """Reject cleanup of a job that still owns an active preview."""
        safe_job = validate_identifier(job_id, "job")
        if not self.gateway.is_dir() or not self.slot_file.is_file():
            return
        with self._lock():
            owned = [route.slot for route in self._load_routes() if route.job_id == safe_job]
        if owned:
            raise ValueError(f"job owns active previews: {', '.join(sorted(owned))}")
