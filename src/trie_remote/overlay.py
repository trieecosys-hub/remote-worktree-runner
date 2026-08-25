"""Discover and apply sparse worktree overlays safely."""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
import subprocess

from trie_remote.job_store import OverlayManifest


def validate_overlay_path(value: str) -> str:
    """Return one safe repository-relative POSIX path."""
    if not value or value.startswith("/") or any(
        character in value for character in ("\0", "\n", "\r")
    ):
        raise ValueError(f"invalid overlay path: {value!r}")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(f"invalid overlay path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"invalid overlay path: {value!r}")
    return path.as_posix()


def _exclude_patterns(exclude_file: Path) -> tuple[str, ...]:
    patterns = []
    for line in exclude_file.read_text(encoding="utf-8").splitlines():
        operation, separator, pattern = line.partition(" ")
        if operation == "-" and separator and pattern:
            patterns.append(pattern)
    return tuple(patterns)


def _is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    components = path.split("/")
    for pattern in patterns:
        directory = pattern.endswith("/")
        normalized = pattern.rstrip("/")
        if "/" in normalized:
            if fnmatch.fnmatchcase(path, normalized) or (
                directory and path.startswith(f"{normalized}/")
            ):
                return True
            continue
        if any(fnmatch.fnmatchcase(component, normalized) for component in components):
            return True
    return False


def _git_bytes(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def _is_submodule(root: Path, path: str) -> bool:
    output = _git_bytes(root, "ls-files", "--stage", "-z", "--", path)
    return output.startswith(b"160000 ")


def discover_overlay(root: Path, exclude_file: Path) -> OverlayManifest:
    """Derive an exact sparse overlay from Git state."""
    transfer: set[str] = set()
    delete: set[str] = set()
    patterns = _exclude_patterns(exclude_file)
    output = _git_bytes(
        root,
        "diff",
        "--name-status",
        "--find-renames",
        "-z",
        "HEAD",
    )
    fields = output.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    index = 0
    while index < len(fields):
        status = fields[index].decode("utf-8", errors="surrogateescape")
        index += 1
        if "\t" in status:
            status, first_path = status.split("\t", 1)
        else:
            first_path = fields[index].decode(
                "utf-8",
                errors="surrogateescape",
            )
            index += 1
        paths = [validate_overlay_path(first_path)]
        if status.startswith(("R", "C")):
            paths.append(
                validate_overlay_path(
                    fields[index].decode("utf-8", errors="surrogateescape"),
                ),
            )
            index += 1
        for path in paths:
            if _is_submodule(root, path):
                raise ValueError(f"submodule overlay is unsupported: {path}")
        if status.startswith("R"):
            if not _is_excluded(paths[0], patterns):
                delete.add(paths[0])
            if not _is_excluded(paths[1], patterns):
                transfer.add(paths[1])
        elif status.startswith("C"):
            if not _is_excluded(paths[1], patterns):
                transfer.add(paths[1])
        elif status.startswith("D"):
            if not _is_excluded(paths[0], patterns):
                delete.add(paths[0])
        elif not _is_excluded(paths[0], patterns):
            transfer.add(paths[0])

    untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        path = validate_overlay_path(
            raw_path.decode("utf-8", errors="surrogateescape"),
        )
        if not _is_excluded(path, patterns):
            transfer.add(path)
    return OverlayManifest(
        transfer=tuple(sorted(transfer)),
        delete=tuple(sorted(delete)),
    )


def apply_overlay_deletions(workspace: Path, paths: tuple[str, ...]) -> None:
    """Delete reserved tracked paths without escaping the workspace."""
    root = workspace.resolve()
    for value in paths:
        relative = validate_overlay_path(value)
        candidate = root.joinpath(*relative.split("/"))
        parent = candidate.parent.resolve()
        if parent != root and not parent.is_relative_to(root):
            raise ValueError(f"overlay deletion escapes workspace: {relative}")
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
        elif candidate.exists():
            raise ValueError(f"overlay deletion is not a file: {relative}")
