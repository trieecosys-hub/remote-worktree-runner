"""Validated models and serialization for stable HTTP previews."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from trie_remote.common import validate_identifier


ROUTE_METADATA_PREFIX = "# remote-worktree-runner-preview: "
HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
IGNORED_GATEWAY_KEYS = frozenset({"TRAEFIK_IMAGE"})
RUNTIME_GATEWAY_KEYS = frozenset(
    {"GATEWAY_BIND_HOST", "GATEWAY_BIND_PORT", "GATEWAY_EDGE_NETWORK"},
)


def validate_hostname(value: str) -> str:
    """Return one normalized exact DNS hostname."""
    if not isinstance(value, str) or len(value) > 253 or value.endswith("."):
        raise ValueError("invalid preview hostname")
    labels = value.split(".")
    if len(labels) < 2 or any(HOST_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        raise ValueError("invalid preview hostname")
    return value


def validate_port(value: int) -> int:
    """Return a valid internal TCP port."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("invalid preview port")
    return value


def validate_check_path(value: str) -> str:
    """Return a safe absolute HTTP check path."""
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise ValueError("invalid preview check path")
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid preview check path")
    return value


@dataclass(frozen=True, slots=True)
class PreviewSlot:
    """One server-approved hostname and repository mapping."""

    slot: str
    hostname: str
    repository: str

    def __post_init__(self) -> None:
        validate_identifier(self.slot, "preview slot")
        validate_hostname(self.hostname)
        validate_identifier(self.repository, "repository")


@dataclass(frozen=True, slots=True)
class GatewayRuntime:
    """Non-secret gateway values needed by the preview registry."""

    bind_host: str
    bind_port: int
    edge_network: str

    def __post_init__(self) -> None:
        if self.bind_host != "127.0.0.1":
            raise ValueError("gateway bind host must be loopback")
        if not 1024 <= validate_port(self.bind_port) <= 65535:
            raise ValueError("gateway bind port must be unprivileged")
        validate_identifier(self.edge_network, "gateway edge network")


@dataclass(frozen=True, slots=True)
class PreviewRoute:
    """Active preview ownership embedded in one Traefik route file."""

    slot: str
    hostname: str
    repository: str
    job_id: str
    project: str
    service: str
    container_id: str
    network_alias: str
    port: int
    check_path: str
    published_at: str

    def __post_init__(self) -> None:
        validate_identifier(self.slot, "preview slot")
        validate_hostname(self.hostname)
        validate_identifier(self.repository, "repository")
        validate_identifier(self.job_id, "job")
        validate_identifier(self.project, "Compose project")
        validate_identifier(self.service, "Compose service")
        if CONTAINER_ID_PATTERN.fullmatch(self.container_id) is None:
            raise ValueError("invalid container ID")
        validate_identifier(self.network_alias, "preview network alias")
        validate_port(self.port)
        validate_check_path(self.check_path)
        try:
            timestamp = datetime.fromisoformat(self.published_at)
        except ValueError as error:
            raise ValueError("invalid publication timestamp") from error
        if timestamp.tzinfo is None:
            raise ValueError("publication timestamp must include a timezone")


def parse_slot_spec(value: str) -> PreviewSlot:
    """Parse `SLOT=HOSTNAME,REPOSITORY` into a validated slot."""
    slot, separator, target = value.partition("=")
    hostname, target_separator, repository = target.partition(",")
    if not separator or not target_separator or "," in repository:
        raise ValueError("preview slot must use SLOT=HOSTNAME,REPOSITORY")
    return PreviewSlot(slot=slot, hostname=hostname, repository=repository)


def _slot_mapping(slot: PreviewSlot) -> dict[str, str]:
    return {"hostname": slot.hostname, "repository": slot.repository}


def _validated_slots(slots: Iterable[PreviewSlot]) -> dict[str, PreviewSlot]:
    result: dict[str, PreviewSlot] = {}
    hostnames: set[str] = set()
    for slot in slots:
        if slot.slot in result:
            raise ValueError(f"duplicate preview slot: {slot.slot}")
        if slot.hostname in hostnames:
            raise ValueError(f"duplicate preview hostname: {slot.hostname}")
        result[slot.slot] = slot
        hostnames.add(slot.hostname)
    return result


def write_slot_configuration(path: Path, slots: Iterable[PreviewSlot]) -> None:
    """Atomically write a deterministic private slot configuration."""
    validated = _validated_slots(slots)
    value = {name: _slot_mapping(validated[name]) for name in sorted(validated)}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_slot_configuration(path: Path) -> dict[str, PreviewSlot]:
    """Load and fully validate one slot configuration file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid preview slot configuration") from error
    if not isinstance(raw, dict):
        raise ValueError("preview slot configuration must be an object")
    slots: list[PreviewSlot] = []
    for name, item in raw.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ValueError("invalid preview slot entry")
        if set(item) != {"hostname", "repository"}:
            raise ValueError("unknown or missing preview slot field")
        slots.append(
            PreviewSlot(
                slot=name,
                hostname=str(item["hostname"]),
                repository=str(item["repository"]),
            ),
        )
    return _validated_slots(slots)


def load_gateway_runtime(path: Path) -> GatewayRuntime:
    """Load only the non-secret runtime fields needed for gateway checks."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("gateway runtime is unavailable") from error
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise ValueError("invalid gateway runtime entry")
        if key in IGNORED_GATEWAY_KEYS:
            continue
        if key not in RUNTIME_GATEWAY_KEYS:
            raise ValueError("unknown gateway runtime key")
        values[key] = value
    if set(values) != RUNTIME_GATEWAY_KEYS:
        raise ValueError("gateway runtime fields are incomplete")
    try:
        bind_port = int(values["GATEWAY_BIND_PORT"])
    except ValueError as error:
        raise ValueError("invalid gateway bind port") from error
    return GatewayRuntime(
        bind_host=values["GATEWAY_BIND_HOST"],
        bind_port=bind_port,
        edge_network=values["GATEWAY_EDGE_NETWORK"],
    )


def render_route(route: PreviewRoute) -> str:
    """Render one deterministic Traefik route with ownership metadata."""
    metadata = json.dumps(asdict(route), sort_keys=True, separators=(",", ":"))
    name = f"preview-{route.slot}"
    return (
        f"{ROUTE_METADATA_PREFIX}{metadata}\n"
        "http:\n"
        "  routers:\n"
        f"    {name}:\n"
        "      entryPoints:\n"
        "        - web\n"
        f'      rule: "Host(`{route.hostname}`)"\n'
        f"      service: {name}\n"
        "  services:\n"
        f"    {name}:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        f'          - url: "http://{route.network_alias}:{route.port}"\n'
    )


def parse_route(content: str) -> PreviewRoute:
    """Parse and authenticate a route generated by `render_route`."""
    first_line, separator, _remainder = content.partition("\n")
    if not separator or not first_line.startswith(ROUTE_METADATA_PREFIX):
        raise ValueError("missing preview ownership metadata")
    try:
        raw = json.loads(first_line.removeprefix(ROUTE_METADATA_PREFIX))
    except json.JSONDecodeError as error:
        raise ValueError("invalid preview ownership metadata") from error
    expected = {field for field in PreviewRoute.__dataclass_fields__}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("unknown or missing preview ownership field")
    route = PreviewRoute(**raw)
    if render_route(route) != content:
        raise ValueError("preview route content does not match ownership metadata")
    return route
