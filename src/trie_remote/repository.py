"""Discover the exact local Git worktree state to synchronize."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
import subprocess

from trie_remote.config import DEFAULT_REPOSITORIES


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class RepositoryState:
    """Identity and dirty state for one local Git worktree."""

    name: str
    root: Path
    git_common_dir: Path
    commit: str
    branch: str | None
    dirty: bool

    @classmethod
    def discover(
        cls,
        cwd: Path,
        allowed_repositories: Collection[str] = DEFAULT_REPOSITORIES,
    ) -> "RepositoryState":
        """Discover a worktree while retaining the product repository name."""
        starting_directory = cwd if cwd.is_dir() else cwd.parent
        root = Path(_git(starting_directory, "rev-parse", "--show-toplevel")).resolve()
        common_value = Path(_git(root, "rev-parse", "--git-common-dir"))
        if not common_value.is_absolute():
            common_value = root / common_value
        git_common_dir = common_value.resolve()
        repository_root = (
            git_common_dir.parent if git_common_dir.name == ".git" else git_common_dir
        )
        name = repository_root.name.removesuffix(".git")
        if name not in allowed_repositories:
            raise ValueError(f"unsupported Trie Platform repository: {name}")

        branch_value = _git(root, "branch", "--show-current")
        return cls(
            name=name,
            root=root,
            git_common_dir=git_common_dir,
            commit=_git(root, "rev-parse", "HEAD"),
            branch=branch_value or None,
            dirty=bool(_git(root, "status", "--porcelain=v1")),
        )

