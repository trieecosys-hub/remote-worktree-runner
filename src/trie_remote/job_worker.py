"""Execute one stored job with durable logs and isolated resources."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time

from trie_remote.config import RunnerConfig
from trie_remote.job_environment import create_job_environment, ensure_job_builder
from trie_remote.job_store import JobStore
from trie_remote.scheduler import DiskGuard, HeavyJobLease
from trie_remote.server_paths import ServerPaths


def run_job(
    paths: ServerPaths,
    job_id: str,
    *,
    create_builder: bool = True,
    disk_guard: DiskGuard | None = None,
) -> int:
    """Run a queued job and persist logs plus exact exit status."""
    store = JobStore(paths)
    spec = store.load(job_id)
    config = RunnerConfig.load(os.environ)
    guard = disk_guard or DiskGuard(
        config.minimum_free_gib,
        config.warning_free_gib,
        config.cancellation_free_gib,
    )
    lease = HeavyJobLease(paths.locks / "heavy.lock", job_id) if spec.weight == "heavy" else None
    if lease is not None:
        lease.acquire()
    try:
        environment = {**os.environ, **create_job_environment(paths, spec)}
        if create_builder:
            ensure_job_builder(
                paths,
                spec,
                lambda argv: subprocess.run(
                    argv,
                    check=True,
                    env=environment,
                    capture_output=True,
                ),
            )
        store.transition(job_id, "running", pid=os.getpid(), started_at=time.time())
        cancellation = store.job_directory(job_id) / "cancel.requested"
        with store.log_path(job_id).open("ab", buffering=0) as log:
            try:
                process = subprocess.Popen(
                    list(spec.argv),
                    cwd=Path(spec.workspace),
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                detail = error.strerror or "operating system error"
                message = (
                    f"[trie-runner] command spawn failed: "
                    f"{type(error).__name__}: {detail}\n"
                )
                log.write(message.encode("utf-8", errors="replace"))
                store.finish(job_id, 127)
                return 127
            warned = False
            while process.poll() is None:
                if cancellation.exists() or guard.monitor(paths.root) == "cancel":
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                    exit_code = int(process.wait())
                    store.transition(
                        job_id,
                        "cancelled",
                        exit_code=exit_code,
                        finished_at=time.time(),
                    )
                    return exit_code
                if not warned and guard.monitor(paths.root) == "warning":
                    log.write(b"[trie-runner] warning: server disk below warning threshold\n")
                    warned = True
                time.sleep(1)
            exit_code = int(process.returncode)
        store.finish(job_id, exit_code)
        return exit_code
    finally:
        if lease is not None:
            lease.release()
