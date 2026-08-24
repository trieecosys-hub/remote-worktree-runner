"""Cloudflare SSH transport for commits and dirty worktree overlays."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from trie_remote.process import ProcessRunner, run_process
from trie_remote.repository import RepositoryState


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

    def push_commit(self, state: RepositoryState, job_id: str, role: str) -> None:
        """Ensure a mirror and push the exact selected commit to a job ref."""
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
        output = (
            result.stdout.decode()
            if isinstance(result.stdout, bytes)
            else result.stdout
        )
        return str(json.loads(output)["workspace"])

    def sync_overlay(self, state: RepositoryState, remote_workspace: str) -> None:
        """Mirror the local worktree, including deletions and untracked source."""
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
