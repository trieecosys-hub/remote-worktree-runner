"""Validated filesystem layout for the server runner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServerPaths:
    """All persistent paths owned by the remote runner."""

    root: Path
    bin: Path
    repos: Path
    workspaces: Path
    jobs: Path
    environments: Path
    toolchains: Path
    caches: Path
    locks: Path

    @classmethod
    def from_root(cls, root: Path) -> "ServerPaths":
        """Create the path model below one explicit root."""
        resolved = root.expanduser().resolve()
        return cls(
            root=resolved,
            bin=resolved / "bin",
            repos=resolved / "repos",
            workspaces=resolved / "workspaces",
            jobs=resolved / "jobs",
            environments=resolved / "environments",
            toolchains=resolved / "toolchains",
            caches=resolved / "caches",
            locks=resolved / "locks",
        )

    def create(self) -> None:
        """Create the server directory layout with private group access."""
        for directory in (
            self.root,
            self.bin,
            self.repos,
            self.workspaces,
            self.jobs,
            self.environments,
            self.toolchains,
            self.caches,
            self.locks,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)
