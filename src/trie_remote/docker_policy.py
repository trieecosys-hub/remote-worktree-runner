"""Guard shared Docker state from daemon-wide job operations."""

from __future__ import annotations

from collections.abc import Sequence


class PolicyError(ValueError):
    """Raised when a Docker command crosses the job safety boundary."""


GLOBAL_FLAGS_WITH_VALUE = {
    "--config",
    "--context",
    "--host",
    "--log-level",
    "-c",
    "-H",
    "-l",
}


def _command_tokens(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    index = 0
    while index < len(values):
        value = values[index]
        if value in GLOBAL_FLAGS_WITH_VALUE:
            index += 2
            continue
        if any(value.startswith(f"{flag}=") for flag in GLOBAL_FLAGS_WITH_VALUE):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return values[index:]
    return []


def _builder_value(arguments: Sequence[str]) -> str | None:
    for index, value in enumerate(arguments):
        if value == "--builder" and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith("--builder="):
            return value.split("=", 1)[1]
    return None


def validate_docker_arguments(argv: Sequence[str], job_builder: str) -> None:
    """Reject operations that can mutate Docker state outside one job."""
    command = _command_tokens(argv)
    if not command:
        raise PolicyError("a Docker command is required")

    command_path = tuple(command[:2])
    if command_path in {
        ("system", "prune"),
        ("volume", "prune"),
        ("builder", "prune"),
        ("context", "rm"),
        ("swarm", "leave"),
    }:
        raise PolicyError(f"daemon-wide Docker operation is blocked: {' '.join(command_path)}")
    if command_path == ("image", "prune") and any(
        value in {"-a", "--all"} for value in command[2:]
    ):
        raise PolicyError("global Docker image pruning is blocked")
    if command_path == ("buildx", "prune"):
        builder = _builder_value(command[2:])
        if builder != job_builder:
            raise PolicyError(f"Buildx prune must name job builder {job_builder}")

