"""Configuration shared by local and server runner modes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from trie_remote.common import validate_identifier

DEFAULT_REPOSITORIES = frozenset(
    {
        "trie-vms",
        "trie-center",
        "trie-process",
        "trie-space",
        "trie-platform-ops",
    },
)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Immutable remote runner configuration."""

    ssh_alias: str
    remote_root: Path
    minimum_free_gib: int
    warning_free_gib: int
    cancellation_free_gib: int
    max_heavy_jobs: int
    allowed_repositories: frozenset[str]

    @classmethod
    def load(cls, environ: Mapping[str, str]) -> RunnerConfig:
        """Load configuration from environment values and safe defaults."""

        def value(public_name: str, legacy_name: str, default: str) -> str:
            return environ.get(public_name, environ.get(legacy_name, default))

        repositories = value(
            "REMOTE_RUNNER_ALLOWED_REPOSITORIES",
            "TRIE_REMOTE_ALLOWED_REPOSITORIES",
            ",".join(sorted(DEFAULT_REPOSITORIES)),
        )
        allowed_repositories = frozenset(
            validate_identifier(item.strip(), "repository")
            for item in repositories.split(",")
            if item.strip()
        )
        if not allowed_repositories:
            raise ValueError("at least one allowed repository is required")
        max_heavy_jobs = int(
            value(
                "REMOTE_RUNNER_MAX_HEAVY_JOBS",
                "TRIE_REMOTE_MAX_HEAVY_JOBS",
                "1",
            ),
        )
        if max_heavy_jobs < 1:
            raise ValueError("max heavy jobs must be positive")
        return cls(
            ssh_alias=value(
                "REMOTE_RUNNER_SSH_ALIAS",
                "TRIE_REMOTE_SSH_ALIAS",
                "trie-docker",
            ),
            remote_root=Path(
                value(
                    "REMOTE_RUNNER_ROOT",
                    "TRIE_REMOTE_ROOT",
                    "/srv/trie-platform",
                ),
            ),
            minimum_free_gib=int(
                value(
                    "REMOTE_RUNNER_MINIMUM_FREE_GIB",
                    "TRIE_REMOTE_MINIMUM_FREE_GIB",
                    "100",
                ),
            ),
            warning_free_gib=int(
                value(
                    "REMOTE_RUNNER_WARNING_FREE_GIB",
                    "TRIE_REMOTE_WARNING_FREE_GIB",
                    "80",
                ),
            ),
            cancellation_free_gib=int(
                value(
                    "REMOTE_RUNNER_CANCELLATION_FREE_GIB",
                    "TRIE_REMOTE_CANCELLATION_FREE_GIB",
                    "60",
                ),
            ),
            max_heavy_jobs=max_heavy_jobs,
            allowed_repositories=allowed_repositories,
        )
