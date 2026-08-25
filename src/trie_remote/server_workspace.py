"""Manage private bare mirrors and isolated server Git worktrees."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from trie_remote.common import ensure_below, validate_identifier
from trie_remote.job_store import JobSpec, OverlayManifest
from trie_remote.overlay import apply_overlay_deletions
from trie_remote.server_paths import ServerPaths


COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")


def _run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_bare_repository(paths: ServerPaths, repository: str) -> Path:
    """Create an allowed bare mirror if it does not exist."""
    name = validate_identifier(repository, "repository")
    mirror = ensure_below(paths.repos, paths.repos / f"{name}.git")
    if not mirror.exists():
        _run("git", "init", "--bare", str(mirror))
    elif not (mirror / "HEAD").is_file():
        raise ValueError(f"repository mirror is not bare: {mirror}")
    return mirror


def prepare_workspace(
    paths: ServerPaths,
    job_id: str,
    repository: str,
    commit: str,
    role: str,
) -> Path:
    """Create a detached, clean worktree for one job role."""
    safe_job = validate_identifier(job_id, "job")
    safe_repository = validate_identifier(repository, "repository")
    safe_role = validate_identifier(role, "role")
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise ValueError("invalid commit SHA")

    mirror = ensure_bare_repository(paths, safe_repository)
    object_check = _run(
        "git",
        "--git-dir",
        str(mirror),
        "cat-file",
        "-e",
        f"{commit}^{{commit}}",
        check=False,
    )
    if object_check.returncode != 0:
        raise ValueError(f"commit is missing from {safe_repository}: {commit}")

    workspace = ensure_below(
        paths.workspaces,
        paths.workspaces / safe_repository / safe_job / safe_role,
    )
    if workspace.exists():
        removal = _run(
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "remove",
            "--force",
            str(workspace),
            check=False,
        )
        if removal.returncode != 0 or workspace.exists():
            raise RuntimeError(
                f"failed to remove registered worktree {workspace}: "
                f"{removal.stderr.strip()}",
            )
    workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    _run("git", "--git-dir", str(mirror), "worktree", "prune")
    _run(
        "git",
        "--git-dir",
        str(mirror),
        "worktree",
        "add",
        "--detach",
        str(workspace),
        commit,
    )
    return workspace


def prepare_all_workspaces(
    paths: ServerPaths,
    spec: JobSpec,
) -> dict[str, Path]:
    """Prepare every reserved role and apply its immutable deletion set."""
    if set(spec.commits) != set(spec.workspaces):
        raise ValueError("prepare-all requires one commit per workspace role")
    prepared: dict[str, Path] = {}
    for role in sorted(spec.workspaces):
        repository = spec.repository if role == "primary" else spec.includes[role]
        workspace = prepare_workspace(
            paths,
            spec.job_id,
            repository,
            spec.commits[role],
            role,
        )
        expected = Path(spec.workspaces[role]).resolve()
        if workspace != expected:
            raise ValueError(f"prepared workspace changed for role: {role}")
        manifest = spec.overlays.get(role, OverlayManifest())
        apply_overlay_deletions(workspace, manifest.delete)
        prepared[role] = workspace
    return prepared
