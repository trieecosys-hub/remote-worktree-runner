"""Local control CLI for Trie Platform remote jobs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from trie_remote.common import validate_identifier
from trie_remote.config import RunnerConfig
from trie_remote.job_store import FINAL_STATES, JobSpec
from trie_remote.repository import RepositoryState
from trie_remote.transport import Transport


def build_parser() -> argparse.ArgumentParser:
    """Build the local CLI parser."""
    parser = argparse.ArgumentParser(prog="trie-run")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--job", required=True)
    run_parser.add_argument(
        "--weight",
        choices=("light", "heavy", "exclusive"),
        default="heavy",
    )
    run_parser.add_argument("--include", action="append", default=[])
    run_parser.add_argument("command_arguments", nargs=argparse.REMAINDER)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("job")

    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("-f", "--follow", action="store_true")
    logs_parser.add_argument("job")

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("job")

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("job")
    cleanup_parser.add_argument("--volumes", action="store_true")

    preview_parser = subparsers.add_parser("preview")
    preview_subparsers = preview_parser.add_subparsers(
        dest="preview_command",
        required=True,
    )
    publish_parser = preview_subparsers.add_parser("publish")
    publish_parser.add_argument("--job", required=True)
    publish_parser.add_argument("--slot", required=True)
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--service", required=True)
    publish_parser.add_argument("--port", required=True, type=int)
    publish_parser.add_argument("--check-path", required=True)
    preview_subparsers.add_parser("list")
    unpublish_parser = preview_subparsers.add_parser("unpublish")
    unpublish_parser.add_argument("--job", required=True)
    unpublish_parser.add_argument("--slot", required=True)

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--show-sync", action="store_true")
    return parser


def validate_requested_command(argv: list[str]) -> None:
    """Reject daemon-wide controls before any source is transferred."""
    if not argv:
        raise ValueError("a command is required after --")
    command = Path(argv[0]).name
    if command in {"systemctl", "service"}:
        raise ValueError(f"remote daemon control is not allowed: {command}")
    if any("docker.sock" in value for value in argv):
        raise ValueError("direct Docker socket access is not allowed")
    if (
        command == "docker"
        and len(argv) >= 3
        and tuple(argv[1:3])
        in {
            ("system", "prune"),
            ("volume", "prune"),
            ("builder", "prune"),
            ("context", "rm"),
            ("swarm", "leave"),
        }
    ):
        raise ValueError("daemon-wide Docker operation is not allowed")


def _parse_includes(values: list[str]) -> dict[str, Path]:
    includes: dict[str, Path] = {}
    for value in values:
        role, separator, raw_path = value.partition("=")
        if not separator or not raw_path:
            raise ValueError(f"include must be ROLE=PATH: {value}")
        safe_role = validate_identifier(role, "role")
        if safe_role == "primary" or safe_role in includes:
            raise ValueError(f"duplicate or reserved include role: {safe_role}")
        includes[safe_role] = Path(raw_path).expanduser().resolve()
    return includes


def _exclude_file() -> Path:
    configured = os.environ.get("TRIE_REMOTE_EXCLUDE_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    installed = Path.home() / ".config" / "trie-platform" / "sync-excludes.txt"
    if installed.is_file():
        return installed
    return Path(__file__).resolve().parents[2] / "config" / "sync-excludes.txt"


def _transport(config: RunnerConfig) -> Transport:
    return Transport(
        ssh_alias=config.ssh_alias,
        remote_root=config.remote_root,
        exclude_file=_exclude_file(),
    )


def run_worktrees(
    primary: Path,
    includes: dict[str, Path],
    job_id: str,
    weight: str,
    argv: list[str],
    transport: Transport,
) -> int:
    """Synchronize exact worktrees, submit a job, and follow its log."""
    validate_identifier(job_id, "job")
    validate_requested_command(argv)
    states = {"primary": RepositoryState.discover(primary)}
    states.update(
        {role: RepositoryState.discover(path) for role, path in includes.items()}
    )
    remote_workspaces = {
        role: str(transport.remote_root / "workspaces" / state.name / job_id / role)
        for role, state in states.items()
    }
    overlays = {
        role: state.overlay(transport.exclude_file) for role, state in states.items()
    }
    include_repositories: dict[str, str] = {}
    for role, state in states.items():
        if role != "primary":
            include_repositories[role] = state.name

    spec = JobSpec(
        job_id=job_id,
        repository=states["primary"].name,
        workspace=remote_workspaces["primary"],
        workspaces=remote_workspaces,
        includes=include_repositories,
        weight=weight,
        argv=tuple(argv),
        created_at=datetime.now(timezone.utc).isoformat(),
        commits={role: state.commit for role, state in states.items()},
        overlays=overlays,
    )
    reservation = transport.reserve(spec)
    try:
        if reservation.protocol_version >= 2:
            if dict(reservation.workspaces) != remote_workspaces:
                raise ValueError("server reservation returned unexpected workspaces")
            for role, state in states.items():
                transport.push_commit(
                    state,
                    job_id,
                    role,
                    ensure_repository=False,
                )
            prepared = transport.prepare_all(job_id)
            if prepared != remote_workspaces:
                raise ValueError("server prepared unexpected workspaces")
            for role, state in states.items():
                transport.sync_overlay(state, prepared[role], overlays[role])
        else:
            for role, state in states.items():
                transport.push_commit(state, job_id, role)
                workspace = transport.prepare_workspace(state, job_id, role)
                if workspace != remote_workspaces[role]:
                    raise ValueError(f"server workspace changed for role: {role}")
                transport.sync_full_overlay(state, workspace)
            transport.ssh(["start"], input_bytes=json.dumps(spec.to_dict()).encode())
    except (Exception, KeyboardInterrupt):
        try:
            cancellation = transport.ssh(["cancel", job_id], check=False)
            if cancellation.returncode != 0:
                print(
                    f"trie-run: preparing job cancellation returned "
                    f"{cancellation.returncode}: {job_id}",
                    file=sys.stderr,
                )
        except Exception as cancellation_error:  # noqa: BLE001
            print(
                f"trie-run: could not cancel preparing job {job_id}: "
                f"{cancellation_error}",
                file=sys.stderr,
            )
        raise
    if reservation.protocol_version >= 2:
        try:
            execution = transport.execute(job_id)
        except KeyboardInterrupt:
            print(
                f"\nExecution stream interrupted; job {job_id} continues on server",
                file=sys.stderr,
            )
            return 130
        print(json.dumps(execution.status, indent=2, sort_keys=True))
        return execution.returncode
    try:
        transport.ssh(["logs", "-f", job_id], capture_output=False)
    except KeyboardInterrupt:
        print(
            f"\nLog follow interrupted; job {job_id} continues on server",
            file=sys.stderr,
        )
    result = transport.ssh(["status", job_id])
    output = (
        result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    )
    status = json.loads(output)
    print(json.dumps(status, indent=2, sort_keys=True))
    if status["state"] not in FINAL_STATES:
        return 130
    return int(status.get("exit_code", 0 if status["state"] == "passed" else 1))


def _doctor(config: RunnerConfig, transport: Transport, show_sync: bool) -> int:
    local = {
        command: shutil.which(command) is not None
        for command in ("git", "rsync", "ssh", "cloudflared")
    }
    result = transport.ssh(["doctor"], check=False)
    output = (
        result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    )
    report = {
        "local": local,
        "server": json.loads(output)
        if result.returncode == 0 and output
        else {"reachable": False},
    }
    if show_sync:
        report["sync_excludes"] = (
            _exclude_file().read_text(encoding="utf-8").splitlines()
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if all(local.values()) and result.returncode == 0 else 1


def _preview(arguments: argparse.Namespace, transport: Transport) -> int:
    """Forward one preview operation to the remote registry."""
    remote_arguments = [f"preview-{arguments.preview_command}"]
    if arguments.preview_command == "publish":
        remote_arguments.extend(
            [
                "--job",
                arguments.job,
                "--slot",
                arguments.slot,
                "--project",
                arguments.project,
                "--service",
                arguments.service,
                "--port",
                str(arguments.port),
                "--check-path",
                arguments.check_path,
            ],
        )
    elif arguments.preview_command == "unpublish":
        remote_arguments.extend(
            ["--job", arguments.job, "--slot", arguments.slot],
        )
    result = transport.ssh(remote_arguments)
    if result.stdout:
        output = (
            result.stdout.decode()
            if isinstance(result.stdout, bytes)
            else result.stdout
        )
        print(output, end="" if output.endswith("\n") else "\n")
    return int(result.returncode)


def main(argv: list[str] | None = None) -> int:
    """Run the local CLI."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0
    config = RunnerConfig.load(os.environ)
    transport = _transport(config)
    try:
        if arguments.command == "run":
            command = list(arguments.command_arguments)
            if command and command[0] == "--":
                command.pop(0)
            return run_worktrees(
                Path.cwd(),
                _parse_includes(arguments.include),
                arguments.job,
                arguments.weight,
                command,
                transport,
            )
        if arguments.command == "doctor":
            return _doctor(config, transport, arguments.show_sync)
        if arguments.command == "preview":
            return _preview(arguments, transport)
        validate_identifier(arguments.job, "job")
        remote_arguments = [arguments.command]
        if arguments.command == "logs" and arguments.follow:
            remote_arguments.append("-f")
        remote_arguments.append(arguments.job)
        if arguments.command == "cleanup" and arguments.volumes:
            remote_arguments.append("--volumes")
        result = transport.ssh(
            remote_arguments,
            check=arguments.command not in {"status", "cancel"},
            capture_output=arguments.command != "logs",
        )
        if result.stdout:
            output = (
                result.stdout.decode()
                if isinstance(result.stdout, bytes)
                else result.stdout
            )
            print(output, end="" if output.endswith("\n") else "\n")
        return int(result.returncode)
    except ValueError as error:
        print(f"trie-run: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        if error.stderr:
            message = (
                error.stderr.decode()
                if isinstance(error.stderr, bytes)
                else error.stderr
            )
            print(message, file=sys.stderr, end="" if message.endswith("\n") else "\n")
        return int(error.returncode or 1)
    except KeyboardInterrupt:
        print(
            "\ntrie-run: local transfer interrupted; "
            "preparing job cancellation was requested",
            file=sys.stderr,
        )
        return 130
