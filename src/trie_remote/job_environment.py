"""Create isolated environment variables and command shims for a job."""

from __future__ import annotations

from collections.abc import Callable
import os
import subprocess

from trie_remote.common import ensure_below, validate_identifier
from trie_remote.server_paths import ServerPaths


def job_resource_name(repository: str, job_id: str) -> str:
    """Return the shared name used for Compose and Buildx resources."""
    return f"trie-{validate_identifier(repository, 'repository')}-{validate_identifier(job_id, 'job')}"


def create_job_environment(paths: ServerPaths, job: object) -> dict[str, str]:
    """Create private job directories, a Docker shim, and environment values."""
    job_id = validate_identifier(str(getattr(job, "job_id")), "job")
    repository = validate_identifier(str(getattr(job, "repository")), "repository")
    workspaces = dict(getattr(job, "workspaces"))
    job_directory = ensure_below(paths.jobs, paths.jobs / job_id)
    shim_directory = job_directory / "bin"
    docker_config = job_directory / "docker-config"
    cache_home = paths.caches / "language" / repository / "xdg"
    playwright_browsers = paths.caches / "playwright"
    process_node_bin = paths.toolchains / "process-node" / "bin"
    center_node_bin = paths.toolchains / "center-node" / "bin"
    go_bin = paths.toolchains / "go" / "bin"
    for directory in (
        job_directory,
        shim_directory,
        docker_config,
        cache_home,
        playwright_browsers,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    shim = shim_directory / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'exec "{paths.bin / "trie-runner"}" docker '
        '--job "$TRIE_REMOTE_JOB_ID" -- "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    pnpm_shim = shim_directory / "pnpm"
    pnpm_shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'exec corepack pnpm "$@"\n',
        encoding="utf-8",
    )
    pnpm_shim.chmod(0o700)

    resource_name = job_resource_name(repository, job_id)
    environment = {
        "TRIE_REMOTE_JOB_ID": job_id,
        "COMPOSE_PROJECT_NAME": resource_name,
        "BUILDX_BUILDER": resource_name,
        "DOCKER_CONFIG": str(docker_config),
        "XDG_CACHE_HOME": str(cache_home),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright_browsers),
        "PLAYWRIGHT_SKIP_BROWSER_GC": "1",
        "TRIE_M4_CENTER_NODE_DIR": str(center_node_bin),
        "PATH": (
            f"{shim_directory}:{paths.bin}:{process_node_bin}:{go_bin}:"
            f"{os.environ.get('PATH', '/usr/bin:/bin')}"
        ),
    }
    for role, workspace in workspaces.items():
        safe_role = validate_identifier(str(role), "role")
        environment[f"TRIE_{safe_role.upper().replace('-', '_')}_ROOT"] = str(workspace)
    if "primary" in workspaces:
        environment["TRIE_PRIMARY_ROOT"] = str(workspaces["primary"])
    return environment


def ensure_job_builder(
    paths: ServerPaths,
    job: object,
    run: Callable[[list[str]], object],
) -> None:
    """Create the exact Buildx builder reserved for a job."""
    del paths
    builder = job_resource_name(
        str(getattr(job, "repository")),
        str(getattr(job, "job_id")),
    )
    try:
        run(["/usr/bin/docker", "buildx", "inspect", builder])
    except subprocess.CalledProcessError:
        run(
            [
                "/usr/bin/docker",
                "buildx",
                "create",
                "--name",
                builder,
                "--driver",
                "docker-container",
                "--driver-opt",
                "default-load=true",
                "--use",
            ],
        )


def cleanup_job_builder(
    paths: ServerPaths,
    job: object,
    run: Callable[..., object],
) -> None:
    """Remove a job builder using the Docker config that owns its metadata."""
    job_id = validate_identifier(str(getattr(job, "job_id")), "job")
    builder = job_resource_name(
        str(getattr(job, "repository")),
        job_id,
    )
    environment = os.environ.copy()
    environment["DOCKER_CONFIG"] = str(paths.jobs / job_id / "docker-config")
    run(
        ["/usr/bin/docker", "buildx", "rm", builder],
        env=environment,
        check=False,
    )
