"""Detect published Compose ports that belong to another job."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import re
import subprocess


class PortConflictError(ValueError):
    """Raised when a published port belongs to another Compose project."""


def validate_published_ports(
    compose_config: Mapping[str, object],
    running_ports: Mapping[tuple[int, str], str],
    project_name: str,
) -> None:
    """Validate published ports from parsed Compose JSON."""
    services = compose_config.get("services", {})
    if not isinstance(services, Mapping):
        return
    for service_name, service_value in services.items():
        if not isinstance(service_value, Mapping):
            continue
        ports = service_value.get("ports", [])
        if not isinstance(ports, list):
            continue
        for port in ports:
            if not isinstance(port, Mapping) or port.get("published") is None:
                continue
            published = int(str(port["published"]))
            protocol = str(port.get("protocol", "tcp"))
            owner = running_ports.get((published, protocol))
            if owner is not None and owner != project_name:
                raise PortConflictError(
                    f"{protocol}/{published} for {service_name} belongs to {owner}",
                )


COMPOSE_FLAGS_WITH_VALUE = {
    "-f",
    "--file",
    "-p",
    "--project-name",
    "--project-directory",
    "--env-file",
    "--profile",
    "--parallel",
    "--progress",
    "--ansi",
}


def _compose_action_index(argv: Sequence[str]) -> int | None:
    try:
        index = list(argv).index("compose") + 1
    except ValueError:
        return None
    values = list(argv)
    while index < len(values):
        value = values[index]
        if value in COMPOSE_FLAGS_WITH_VALUE:
            index += 2
            continue
        if any(value.startswith(f"{flag}=") for flag in COMPOSE_FLAGS_WITH_VALUE):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return index
    return None


def compose_project_name(argv: Sequence[str], default: str) -> str:
    """Return a validated explicit Compose project or the job default."""
    values = list(argv)
    for index, value in enumerate(values):
        if value in {"-p", "--project-name"} and index + 1 < len(values):
            project = values[index + 1]
            break
        if value.startswith("--project-name="):
            project = value.split("=", 1)[1]
            break
    else:
        project = default
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project) is None:
        raise PortConflictError(f"invalid Compose project: {project}")
    return project


def validate_compose_command(
    argv: Sequence[str],
    project_name: str,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    """Inspect Compose configuration and live owners before publishing ports."""
    values = list(argv)
    action_index = _compose_action_index(values)
    if action_index is None or values[action_index] not in {"up", "run", "start"}:
        return None
    effective_project = compose_project_name(values, project_name)

    config_command = ["/usr/bin/docker", *values[:action_index], "config", "--format", "json"]
    config_result = run(config_command, check=True, capture_output=True, text=True)
    compose_config = json.loads(config_result.stdout)
    ps_result = run(
        ["/usr/bin/docker", "ps", "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    container_ids = ps_result.stdout.split()
    running_ports: dict[tuple[int, str], str] = {}
    if container_ids:
        inspect_result = run(
            ["/usr/bin/docker", "inspect", *container_ids],
            check=True,
            capture_output=True,
            text=True,
        )
        for container in json.loads(inspect_result.stdout):
            labels = container.get("Config", {}).get("Labels") or {}
            owner = labels.get("com.docker.compose.project", "unmanaged-container")
            ports = container.get("NetworkSettings", {}).get("Ports") or {}
            for container_port, bindings in ports.items():
                protocol = str(container_port).rsplit("/", 1)[-1]
                for binding in bindings or []:
                    host_port = binding.get("HostPort")
                    if host_port:
                        running_ports[(int(host_port), protocol)] = owner
    validate_published_ports(compose_config, running_ports, effective_project)
    return effective_project
