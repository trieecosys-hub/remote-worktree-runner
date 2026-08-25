"""Cloudflare SSH transport for commits and dirty worktree overlays."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trie_remote.job_store import JobSpec, OverlayManifest
from trie_remote.process import ProcessRunner, run_process
from trie_remote.repository import RepositoryState


@dataclass(frozen=True, slots=True)
class Reservation:
    """Server capabilities and paths returned by one reservation."""

    protocol_version: int
    mirrors: Mapping[str, str]
    workspaces: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Final status returned by one attached execution session."""

    returncode: int
    status: dict[str, Any]


class Transport:
    """Construct exact local process calls for one configured server."""

    def __init__(
        self,
        *,
        ssh_alias: str,
        remote_root: Path,
        exclude_file: Path,
        run: ProcessRunner = run_process,
    ) -> None:
        self.ssh_alias = ssh_alias
        self.remote_root = remote_root
        self.exclude_file = exclude_file
        self.run = run

    @property
    def remote_runner(self) -> str:
        """Return the installed server entrypoint."""
        return str(self.remote_root / "bin" / "trie-runner")

    def ssh(
        self,
        argv: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        """Invoke the server runner without an intermediate shell."""
        return self.run(
            ["ssh", self.ssh_alias, self.remote_runner, *argv],
            input=input_bytes,
            check=check,
            capture_output=capture_output,
        )

    def reserve(self, spec: JobSpec) -> Reservation:
        """Reserve one job and discover the server transfer protocol."""
        result = self.ssh(
            ["reserve"],
            input_bytes=json.dumps(spec.to_dict()).encode(),
        )
        response = self._json_output(result)
        return Reservation(
            protocol_version=int(response.get("protocol_version", 1)),
            mirrors={
                str(role): str(path)
                for role, path in dict(response.get("mirrors", {})).items()
            },
            workspaces={
                str(role): str(path)
                for role, path in dict(response.get("workspaces", {})).items()
            },
        )

    def push_commit(
        self,
        state: RepositoryState,
        job_id: str,
        role: str,
        *,
        ensure_repository: bool = True,
    ) -> None:
        """Ensure a mirror and push the exact selected commit to a job ref."""
        if ensure_repository:
            self.ssh(["ensure-repo", state.name])
        self.run(
            [
                "git",
                "push",
                "--no-verify",
                "--force",
                f"{self.ssh_alias}:{self.remote_root}/repos/{state.name}.git",
                f"{state.commit}:refs/trie-jobs/{job_id}/{role}",
            ],
            cwd=state.root,
            check=True,
            capture_output=True,
        )

    def workspace_path(self, state: RepositoryState, job_id: str, role: str) -> str:
        """Resolve the deterministic server workspace before source transfer."""
        result = self.ssh(
            [
                "workspace-path",
                "--job",
                job_id,
                "--repository",
                state.name,
                "--role",
                role,
            ],
        )
        output = (
            result.stdout.decode()
            if isinstance(result.stdout, bytes)
            else result.stdout
        )
        return str(json.loads(output)["workspace"])

    def prepare_workspace(self, state: RepositoryState, job_id: str, role: str) -> str:
        """Create the detached server worktree and return its path."""
        result = self.ssh(
            [
                "prepare-workspace",
                "--job",
                job_id,
                "--repository",
                state.name,
                "--commit",
                state.commit,
                "--role",
                role,
            ],
        )
        return str(self._json_output(result)["workspace"])

    def prepare_all(self, job_id: str) -> dict[str, str]:
        """Prepare every reserved role in one server call."""
        result = self.ssh(["prepare-all", "--job", job_id])
        response = self._json_output(result)
        return {
            str(role): str(path) for role, path in dict(response["workspaces"]).items()
        }

    def sync_overlay(
        self,
        state: RepositoryState,
        remote_workspace: str,
        manifest: OverlayManifest,
    ) -> None:
        """Transfer only changed and untracked files from one worktree."""
        if not manifest.transfer:
            return
        file_list = (
            b"\0".join(
                path.encode("utf-8", errors="surrogateescape")
                for path in manifest.transfer
            )
            + b"\0"
        )
        self.run(
            [
                "rsync",
                "-az",
                "--from0",
                "--files-from=-",
                "--relative",
                "--safe-links",
                f"--filter=merge {self.exclude_file}",
                "-e",
                "ssh",
                f"{state.root}/",
                f"{self.ssh_alias}:{remote_workspace}/",
            ],
            input=file_list,
            check=True,
            capture_output=True,
        )

    def sync_full_overlay(
        self,
        state: RepositoryState,
        remote_workspace: str,
    ) -> None:
        """Use the protocol-v1 full-tree synchronization contract."""
        self.run(
            [
                "rsync",
                "-az",
                "--delete",
                "--safe-links",
                f"--filter=merge {self.exclude_file}",
                "-e",
                "ssh",
                f"{state.root}/",
                f"{self.ssh_alias}:{remote_workspace}/",
            ],
            check=True,
            capture_output=True,
        )

    def execute(self, job_id: str) -> ExecutionResult:
        """Start or reattach to a job through one streaming SSH session."""
        result = self.run(
            [
                "ssh",
                self.ssh_alias,
                self.remote_runner,
                "execute",
                "--job",
                job_id,
            ],
            stdout=subprocess.PIPE,
            check=False,
        )
        response = self._json_output(result)
        return ExecutionResult(int(result.returncode), response)

    @staticmethod
    def _json_output(result: subprocess.CompletedProcess[Any]) -> dict[str, Any]:
        """Decode one JSON object returned by the server."""
        output = (
            result.stdout.decode()
            if isinstance(result.stdout, bytes)
            else result.stdout
        )
        return dict(json.loads(output))
