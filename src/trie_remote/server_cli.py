"""Server-side CLI for Trie Platform remote jobs."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from trie_remote.common import ensure_below, validate_identifier
from trie_remote.config import RunnerConfig
from trie_remote.docker_policy import validate_docker_arguments
from trie_remote.job_environment import cleanup_job_builder, job_resource_name
from trie_remote.job_store import FINAL_STATES, JobSpec, JobStore, utc_now
from trie_remote.job_worker import run_job
from trie_remote.port_policy import PortConflictError, validate_compose_command
from trie_remote.preview_registry import PreviewRegistry
from trie_remote.scheduler import DiskGuard, ResourcePool
from trie_remote.scheduler import unit_is_inactive as _unit_is_inactive
from trie_remote.server_paths import ServerPaths
from trie_remote.server_workspace import (
    ensure_bare_repository,
    prepare_all_workspaces,
    prepare_workspace,
)


def _kernel_integer(relative_path: str) -> int | None:
    """Read one integer from the Linux procfs sysctl hierarchy."""
    try:
        value = (Path("/proc/sys") / relative_path).read_text(encoding="utf-8")
        return int(value.strip())
    except (OSError, ValueError):
        return None


def _resource_pool(
    config: RunnerConfig,
    paths: ServerPaths,
    store: JobStore,
) -> ResourcePool:
    """Build the configured scheduler with systemd stale-worker detection."""
    return ResourcePool(
        paths.locks / "heavy-pool",
        config.max_heavy_jobs,
        store,
        lambda job_id: _unit_is_inactive(
            str(
                store.status(job_id).get(
                    "unit",
                    f"trie-job-{job_id}",
                ),
            ),
        ),
    )


def _reconcile_inactive_unit(
    store: JobStore,
    job_id: str,
    status: dict[str, object],
) -> dict[str, object] | None:
    """Finalize a non-final job whose transient worker unit is gone."""
    unit = str(status.get("unit", f"trie-job-{job_id}"))
    if not _unit_is_inactive(unit):
        return None
    latest = store.status(job_id)
    if latest["state"] in FINAL_STATES:
        return latest
    try:
        return store.transition(
            job_id,
            "cancelled",
            exit_code=127,
            reason="worker unit is no longer active",
            finished_at=utc_now(),
        )
    except ValueError:
        latest = store.status(job_id)
        if latest["state"] in FINAL_STATES:
            return latest
        raise


def _validate_spec(config: RunnerConfig, paths: ServerPaths, spec: JobSpec) -> None:
    """Validate repositories and workspace paths in a submitted job contract."""
    repositories = {spec.repository, *spec.includes.values()}
    disallowed = sorted(repositories - config.allowed_repositories)
    if disallowed:
        raise ValueError(f"repository not allowed: {disallowed[0]}")
    if spec.workspaces.get("primary") != spec.workspace:
        raise ValueError("primary workspace does not match workspace")
    expected_roles = {"primary", *spec.includes}
    if set(spec.workspaces) != expected_roles:
        raise ValueError("workspace roles do not match included repositories")
    for role, workspace in spec.workspaces.items():
        repository = spec.repository if role == "primary" else spec.includes[role]
        expected = ensure_below(
            paths.workspaces,
            paths.workspaces / repository / spec.job_id / role,
        )
        if ensure_below(paths.workspaces, Path(workspace)) != expected:
            raise ValueError(f"unexpected workspace path for role: {role}")


def _missing_job(job_id: str) -> dict[str, object]:
    """Return a stable response for a job that may still be uploading."""
    return {"job_id": job_id, "state": "not-found", "retryable": True}


def _start_job(
    config: RunnerConfig,
    paths: ServerPaths,
    store: JobStore,
    spec: JobSpec,
) -> dict[str, object]:
    """Admit one prepared job and launch its reconnectable worker."""
    reserved = store.exists(spec.job_id)
    if reserved:
        if store.load(spec.job_id) != spec:
            raise ValueError("job reservation does not match start request")
        status = store.status(spec.job_id)
        if status["state"] != "preparing":
            if status["state"] in FINAL_STATES:
                return status
            raise ValueError("only a preparing job can be started")
    guard = DiskGuard(
        config.minimum_free_gib,
        config.warning_free_gib,
        config.cancellation_free_gib,
    )
    free_bytes = guard.admit(paths.root, spec.weight)
    if not reserved:
        store.create(spec)
    unit = f"trie-job-{spec.job_id}"
    status = store.transition(
        spec.job_id,
        "queued",
        unit=unit,
        admitted_free_bytes=free_bytes,
    )
    runner = str(paths.bin / "trie-runner")
    try:
        subprocess.run(
            [
                "systemd-run",
                "--user",
                "--unit",
                unit,
                "--collect",
                "--quiet",
                runner,
                "worker",
                "--job",
                spec.job_id,
            ],
            check=True,
        )
    except subprocess.CalledProcessError:
        store.transition(spec.job_id, "cancelled", reason="systemd start failed")
        raise
    return {**status, "job_id": spec.job_id, "unit": unit}


def _execute_job(
    config: RunnerConfig,
    paths: ServerPaths,
    store: JobStore,
    job_id: str,
) -> int:
    """Start if needed, stream logs, and return the exact final exit code."""
    spec = store.load(job_id)
    status = store.status(job_id)
    if status["state"] == "preparing":
        status = _start_job(config, paths, store, spec)

    log_path = store.log_path(job_id)
    position = 0
    while True:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(position)
            content = stream.read()
            position = stream.tell()
        if content:
            print(content, end="", flush=True, file=sys.stderr)
        status = store.status(job_id)
        if status["state"] in FINAL_STATES:
            print(json.dumps(status, sort_keys=True))
            return int(
                status.get(
                    "exit_code",
                    0 if status["state"] == "passed" else 1,
                ),
            )
        time.sleep(1)


def build_parser() -> argparse.ArgumentParser:
    """Build the server CLI parser."""
    parser = argparse.ArgumentParser(prog="trie-runner")
    subparsers = parser.add_subparsers(dest="command")

    ensure_parser = subparsers.add_parser("ensure-repo")
    ensure_parser.add_argument("repository")

    prepare_parser = subparsers.add_parser("prepare-workspace")
    prepare_parser.add_argument("--job", required=True)
    prepare_parser.add_argument("--repository", required=True)
    prepare_parser.add_argument("--commit", required=True)
    prepare_parser.add_argument("--role", required=True)

    prepare_all_parser = subparsers.add_parser("prepare-all")
    prepare_all_parser.add_argument("--job", required=True)

    workspace_parser = subparsers.add_parser("workspace-path")
    workspace_parser.add_argument("--job", required=True)
    workspace_parser.add_argument("--repository", required=True)
    workspace_parser.add_argument("--role", required=True)

    docker_parser = subparsers.add_parser("docker")
    docker_parser.add_argument("--job", required=True)
    docker_parser.add_argument("docker_arguments", nargs=argparse.REMAINDER)

    subparsers.add_parser("reserve")
    subparsers.add_parser("start")

    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--job", required=True)

    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--job", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("job")

    logs_parser = subparsers.add_parser("logs")
    logs_parser.add_argument("job")
    logs_parser.add_argument("-f", "--follow", action="store_true")

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("job")

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("job")
    cleanup_parser.add_argument("--volumes", action="store_true")

    preview_publish_parser = subparsers.add_parser("preview-publish")
    preview_publish_parser.add_argument("--job", required=True)
    preview_publish_parser.add_argument("--slot", required=True)
    preview_publish_parser.add_argument("--project", required=True)
    preview_publish_parser.add_argument("--service", required=True)
    preview_publish_parser.add_argument("--port", required=True, type=int)
    preview_publish_parser.add_argument("--check-path", required=True)
    subparsers.add_parser("preview-list")
    preview_unpublish_parser = subparsers.add_parser("preview-unpublish")
    preview_unpublish_parser.add_argument("--job", required=True)
    preview_unpublish_parser.add_argument("--slot", required=True)
    subparsers.add_parser("doctor")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the server CLI."""
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0

    config = RunnerConfig.load(os.environ)
    paths = ServerPaths.from_root(config.remote_root)
    paths.create()
    store = JobStore(paths)
    if arguments.command == "preview-publish":
        try:
            route = PreviewRegistry(paths).publish(
                job_id=arguments.job,
                slot=arguments.slot,
                project=arguments.project,
                service=arguments.service,
                port=arguments.port,
                check_path=arguments.check_path,
            )
        except ValueError as error:
            print(
                json.dumps(
                    {
                        "error": "preview-publish-rejected",
                        "message": str(error),
                    },
                    sort_keys=True,
                ),
            )
            return 2
        print(json.dumps(asdict(route), sort_keys=True))
        return 0

    if arguments.command == "preview-list":
        routes = PreviewRegistry(paths).list()
        print(json.dumps([asdict(route) for route in routes], sort_keys=True))
        return 0

    if arguments.command == "preview-unpublish":
        route = PreviewRegistry(paths).unpublish(arguments.job, arguments.slot)
        print(json.dumps(asdict(route), sort_keys=True))
        return 0

    if arguments.command == "ensure-repo":
        mirror = ensure_bare_repository(paths, arguments.repository)
        print(json.dumps({"repository": arguments.repository, "mirror": str(mirror)}))
        return 0

    if arguments.command == "prepare-workspace":
        workspace = prepare_workspace(
            paths,
            arguments.job,
            arguments.repository,
            arguments.commit,
            arguments.role,
        )
        print(
            json.dumps(
                {
                    "repository": arguments.repository,
                    "job_id": arguments.job,
                    "role": arguments.role,
                    "workspace": str(workspace),
                    "commit": arguments.commit,
                },
            ),
        )
        return 0

    if arguments.command == "prepare-all":
        spec = store.load(arguments.job)
        prepared = prepare_all_workspaces(paths, spec)
        print(
            json.dumps(
                {
                    "job_id": spec.job_id,
                    "workspaces": {
                        role: str(workspace) for role, workspace in prepared.items()
                    },
                },
                sort_keys=True,
            ),
        )
        return 0

    if arguments.command == "docker":
        docker_arguments = list(arguments.docker_arguments)
        if docker_arguments and docker_arguments[0] == "--":
            docker_arguments.pop(0)
        metadata_file = paths.jobs / arguments.job / "metadata.json"
        if not metadata_file.is_file():
            raise ValueError(f"unknown job: {arguments.job}")
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        builder = job_resource_name(metadata["repository"], arguments.job)
        validate_docker_arguments(docker_arguments, builder)
        try:
            compose_project = validate_compose_command(
                docker_arguments,
                builder,
                subprocess.run,
            )
        except PortConflictError as error:
            print(f"trie-runner: {error}", file=sys.stderr)
            return 2
        if compose_project is not None:
            project_file = paths.jobs / arguments.job / "compose-projects.json"
            projects = (
                json.loads(project_file.read_text(encoding="utf-8"))
                if project_file.is_file()
                else []
            )
            if compose_project not in projects:
                projects.append(compose_project)
                project_file.write_text(
                    json.dumps(projects, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                project_file.chmod(0o600)
        os.execv("/usr/bin/docker", ["docker", *docker_arguments])

    if arguments.command == "doctor":

        def version(command: list[str]) -> str | None:
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError):
                return None
            return (result.stdout or result.stderr).strip().splitlines()[0]

        systemd_linger = (
            version(
                [
                    "/usr/bin/loginctl",
                    "show-user",
                    str(os.getuid()),
                    "--property=Linger",
                    "--value",
                ],
            )
            == "yes"
        )
        scheduler = _resource_pool(config, paths, store).snapshot()
        inotify_max_user_instances = _kernel_integer("fs/inotify/max_user_instances")
        inotify_max_user_watches = _kernel_integer("fs/inotify/max_user_watches")
        inotify_max_queued_events = _kernel_integer("fs/inotify/max_queued_events")
        inotify_ready = (
            inotify_max_user_instances is not None
            and inotify_max_user_instances >= 8192
            and inotify_max_user_watches is not None
            and inotify_max_user_watches >= 1048576
            and inotify_max_queued_events is not None
            and inotify_max_queued_events >= 32768
        )
        report = {
            "reachable": True,
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "docker": version(
                ["/usr/bin/docker", "version", "--format", "{{.Server.Version}}"]
            ),
            "compose": version(["/usr/bin/docker", "compose", "version", "--short"]),
            "buildx": version(["/usr/bin/docker", "buildx", "version"]),
            "kubectl": version(
                [str(paths.bin / "kubectl"), "version", "--client", "--output=yaml"]
            ),
            "kind": version([str(paths.bin / "kind"), "version"]),
            "jq": version([str(paths.bin / "jq"), "--version"]),
            "systemd_user": version(["systemctl", "--user", "is-system-running"]),
            "systemd_linger": systemd_linger,
            "inotify_max_user_instances": inotify_max_user_instances,
            "inotify_max_user_watches": inotify_max_user_watches,
            "inotify_max_queued_events": inotify_max_queued_events,
            "inotify_ready": inotify_ready,
            "scheduler_capacity": scheduler["capacity"],
            "scheduler_held_permits": scheduler["held_permits"],
            "scheduler_queued_jobs": scheduler["queued_jobs"],
            "scheduler_exclusive_waiting": scheduler["exclusive_waiting"],
            "free_bytes": shutil.disk_usage(paths.root).free,
            "remote_root": str(paths.root),
        }
        print(json.dumps(report, sort_keys=True))
        return (
            0
            if report["docker"]
            and report["systemd_user"]
            and systemd_linger
            and inotify_ready
            else 1
        )

    if arguments.command == "reserve":
        spec = JobSpec.from_dict(json.load(sys.stdin))
        _validate_spec(config, paths, spec)
        repositories = {
            "primary": spec.repository,
            **dict(spec.includes),
        }
        mirrors = {
            role: str(ensure_bare_repository(paths, repository))
            for role, repository in repositories.items()
        }
        store.create(spec)
        print(
            json.dumps(
                {
                    "protocol_version": 2,
                    "job_id": spec.job_id,
                    "state": "preparing",
                    "mirrors": mirrors,
                    "workspaces": dict(spec.workspaces),
                },
                sort_keys=True,
            ),
        )
        return 0

    if arguments.command == "start":
        spec = JobSpec.from_dict(json.load(sys.stdin))
        _validate_spec(config, paths, spec)
        status = _start_job(config, paths, store, spec)
        print(json.dumps(status, sort_keys=True))
        return 0

    if arguments.command == "execute":
        return _execute_job(config, paths, store, arguments.job)

    if arguments.command == "worker":
        return run_job(paths, arguments.job)

    if arguments.command == "status":
        if not store.exists(arguments.job):
            print(json.dumps(_missing_job(arguments.job), sort_keys=True))
            return 3
        status = store.status(arguments.job)
        if status["state"] == "queued":
            scheduler = _resource_pool(config, paths, store).snapshot(arguments.job)
            for key in (
                "queue_position",
                "queue_wait_seconds",
                "requested_permits",
                "available_permits",
                "blocked_by",
            ):
                if key in scheduler:
                    status[key] = scheduler[key]
        print(json.dumps(status, sort_keys=True))
        return 0

    if arguments.command == "logs":
        log_path = store.log_path(arguments.job)
        position = 0
        while True:
            with log_path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(position)
                content = stream.read()
                position = stream.tell()
            if content:
                print(content, end="", flush=True)
            status = store.status(arguments.job)
            if not arguments.follow or status["state"] in FINAL_STATES:
                return 0
            time.sleep(1)

    if arguments.command == "cancel":
        if not store.exists(arguments.job):
            print(json.dumps(_missing_job(arguments.job), sort_keys=True))
            return 3
        status = store.status(arguments.job)
        if status["state"] in FINAL_STATES:
            print(json.dumps(status, sort_keys=True))
            return 0
        if status["state"] == "preparing":
            status = store.transition(
                arguments.job,
                "cancelled",
                exit_code=130,
                reason="cancelled before worker start",
                finished_at=utc_now(),
            )
            print(json.dumps(status, sort_keys=True))
            return 0
        reconciled = _reconcile_inactive_unit(store, arguments.job, status)
        if reconciled is not None:
            print(json.dumps(reconciled, sort_keys=True))
            return 0
        store.request_cancel(arguments.job)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = store.status(arguments.job)
            if status["state"] in FINAL_STATES:
                print(json.dumps(status, sort_keys=True))
                return 0
            time.sleep(1)
        reconciled = _reconcile_inactive_unit(store, arguments.job, status)
        if reconciled is not None:
            print(json.dumps(reconciled, sort_keys=True))
            return 0
        unit = str(status.get("unit", f"trie-job-{arguments.job}"))
        subprocess.run(
            ["systemctl", "--user", "kill", "--signal=SIGTERM", unit],
            check=False,
        )
        print(json.dumps({"job_id": arguments.job, "state": "cancellation-requested"}))
        return 0

    if arguments.command == "cleanup":
        spec = store.load(arguments.job)
        status = store.status(arguments.job)
        if status["state"] not in FINAL_STATES:
            raise ValueError("only a final job can be cleaned")
        PreviewRegistry(paths).assert_cleanup_allowed(spec.job_id)
        resource = job_resource_name(spec.repository, spec.job_id)
        project_file = store.job_directory(spec.job_id) / "compose-projects.json"
        projects = [resource]
        if project_file.is_file():
            projects.extend(json.loads(project_file.read_text(encoding="utf-8")))
        workspace_exists = Path(spec.workspace).is_dir()
        for project in dict.fromkeys(projects):
            compose = [
                "/usr/bin/docker",
                "compose",
                "-p",
                project,
                "down",
                "--remove-orphans",
            ]
            if arguments.volumes:
                compose.append("--volumes")
            if workspace_exists:
                subprocess.run(compose, cwd=spec.workspace, check=False)
        cleanup_job_builder(paths, spec, subprocess.run)
        for role, workspace in spec.workspaces.items():
            candidate = ensure_below(paths.workspaces, Path(workspace))
            repository = spec.repository if role == "primary" else spec.includes[role]
            mirror = paths.repos / f"{repository}.git"
            subprocess.run(
                [
                    "git",
                    f"--git-dir={mirror}",
                    "worktree",
                    "remove",
                    "--force",
                    str(candidate),
                ],
                check=False,
            )
            workspace_job = paths.workspaces / repository / spec.job_id
            if workspace_job.exists():
                shutil.rmtree(ensure_below(paths.workspaces, workspace_job))
        print(
            json.dumps(
                {"job_id": spec.job_id, "cleaned": True, "volumes": arguments.volumes}
            )
        )
        return 0

    repository = validate_identifier(arguments.repository, "repository")
    job = validate_identifier(arguments.job, "job")
    role = validate_identifier(arguments.role, "role")
    workspace = ensure_below(
        paths.workspaces,
        paths.workspaces / repository / job / role,
    )
    print(
        json.dumps(
            {
                "repository": arguments.repository,
                "job_id": arguments.job,
                "role": arguments.role,
                "workspace": str(Path(workspace)),
            },
        ),
    )
    return 0
