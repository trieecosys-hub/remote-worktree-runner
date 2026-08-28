"""Heavy-job locking and disk admission policy."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from trie_remote.common import validate_identifier
from trie_remote.job_store import FINAL_STATES

if TYPE_CHECKING:
    from trie_remote.job_store import JobStore


def unit_is_inactive(unit: str) -> bool:
    """Return whether systemd confirms that a worker unit cannot be running."""
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState",
                "--property=ActiveState",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    properties = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties.get("LoadState") == "not-found" or properties.get(
        "ActiveState",
    ) in {"inactive", "failed"}


class DiskGuard:
    """Apply runner disk thresholds without global cleanup."""

    def __init__(
        self,
        minimum_gib: int,
        warning_gib: int,
        cancellation_gib: int,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.minimum = minimum_gib * 1024**3
        self.warning = warning_gib * 1024**3
        self.cancellation = cancellation_gib * 1024**3
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)

    def snapshot(self, path: Path) -> int:
        """Return currently available bytes."""
        return int(self._free_bytes(path))

    def admit(self, path: Path, weight: str) -> int:
        """Reject heavy work when the admission threshold is not met."""
        free = self.snapshot(path)
        if weight in {"heavy", "exclusive"} and free < self.minimum:
            raise RuntimeError(
                f"heavy job rejected: {free // 1024**3} GiB free, "
                f"{self.minimum // 1024**3} GiB required",
            )
        return free

    def monitor(self, path: Path) -> str:
        """Classify current disk pressure."""
        free = self.snapshot(path)
        if free < self.cancellation:
            return "cancel"
        if free < self.warning:
            return "warning"
        return "healthy"


class HeavyJobLease:
    """An advisory server-wide lease for one heavy workload."""

    def __init__(self, path: Path, job_id: str) -> None:
        self.path = path
        self.job_id = job_id
        self._stream: BinaryIO | None = None

    def acquire(self, *, blocking: bool = True) -> None:
        """Acquire the lock and record its holder."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        stream = self.path.open("a+b")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(stream.fileno(), flags)
        except BaseException:
            stream.close()
            raise
        stream.seek(0)
        stream.truncate()
        stream.write(f"{self.job_id}\n".encode())
        stream.flush()
        os.fsync(stream.fileno())
        self._stream = stream

    def release(self) -> None:
        """Release the lease when held."""
        if self._stream is None:
            return
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None

    def __enter__(self) -> HeavyJobLease:  # noqa: PYI034
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class SchedulerCancelled(RuntimeError):
    """Raised when a queued job is cancelled before admission."""


class ResourceLease:
    """Kernel-backed permits retained by one admitted worker."""

    def __init__(self, streams: list[BinaryIO]) -> None:
        self._streams = streams

    def release(self) -> None:
        """Release every held permit."""
        while self._streams:
            stream = self._streams.pop()
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def __enter__(self) -> ResourceLease:  # noqa: PYI034
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class ResourcePool:
    """Durable FIFO admission over a fixed set of kernel-backed permits."""

    def __init__(
        self,
        root: Path,
        capacity: int,
        store: JobStore | None = None,
        worker_inactive: Callable[[str], bool] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("resource pool capacity must be positive")
        self.root = root
        self.capacity = capacity
        self.store = store
        self.worker_inactive = worker_inactive
        self.queue = root / "queue"
        self.slots = root / "slots"
        self.queue_lock = root / "queue.lock"
        self.queue.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.slots.mkdir(parents=True, exist_ok=True, mode=0o750)
        for index in range(capacity):
            (self.slots / f"{index}.lock").touch(mode=0o640, exist_ok=True)

    @contextmanager
    def _locked_queue(self) -> Iterator[None]:
        stream = self.queue_lock.open("a+b")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def _ticket_paths(self) -> list[Path]:
        return sorted(self.queue.glob("*.json"), key=lambda path: path.name)

    def _read_ticket(self, path: Path) -> dict[str, Any]:
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def _find_ticket(self, job_id: str) -> Path | None:
        suffix = f"-{job_id}.json"
        return next(
            (path for path in self._ticket_paths() if path.name.endswith(suffix)),
            None,
        )

    def _write_ticket(
        self,
        job_id: str,
        requested_permits: int,
        session: str | None,
    ) -> Path:
        existing = self._find_ticket(job_id)
        if existing is not None:
            return existing
        created_at = time.time()
        path = self.queue / f"{time.time_ns():020d}-{job_id}.json"
        descriptor, temporary = tempfile.mkstemp(dir=self.queue, prefix=".ticket-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "job_id": job_id,
                        "requested_permits": requested_permits,
                        "created_at": created_at,
                        "session": session,
                    },
                    stream,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path

    def _ticket_is_stale(self, job_id: str) -> bool:
        if self.store is None:
            return False
        if not self.store.exists(job_id):
            return True
        if self.store.status(job_id).get("state") in FINAL_STATES:
            return True
        return self.worker_inactive is not None and self.worker_inactive(job_id)

    def _prune_stale_tickets(self) -> None:
        for path in self._ticket_paths():
            try:
                ticket = self._read_ticket(path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                path.unlink(missing_ok=True)
                continue
            if self._ticket_is_stale(str(ticket.get("job_id", ""))):
                path.unlink(missing_ok=True)

    def _try_slots(self, job_id: str, requested: int) -> ResourceLease | None:
        acquired: list[BinaryIO] = []
        for path in sorted(self.slots.glob("*.lock"), key=lambda item: item.name):
            stream = path.open("a+b")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                continue
            stream.seek(0)
            stream.truncate()
            stream.write(f"{job_id}\n".encode())
            stream.flush()
            acquired.append(stream)
            if len(acquired) == requested:
                return ResourceLease(acquired)
        for stream in reversed(acquired):
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        return None

    def _remove_ticket(self, job_id: str) -> None:
        ticket = self._find_ticket(job_id)
        if ticket is not None:
            ticket.unlink(missing_ok=True)

    def _supersede_queued_session(self, job_id: str, session: str | None) -> None:
        """Cancel older queued work from one explicitly identified session."""
        if session is None or self.store is None:
            return
        for path in self._ticket_paths():
            try:
                ticket = self._read_ticket(path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                path.unlink(missing_ok=True)
                continue
            older_job = str(ticket.get("job_id", ""))
            if older_job == job_id or ticket.get("session") != session:
                continue
            if self._ticket_is_stale(older_job):
                path.unlink(missing_ok=True)
                continue
            self.store.request_cancel(older_job)
            path.unlink(missing_ok=True)

    def wait(
        self,
        job_id: str,
        weight: str,
        cancelled: Callable[[], bool],
        *,
        session: str | None = None,
    ) -> ResourceLease:
        """Wait in FIFO order and return the permits required by a job."""
        safe_job = validate_identifier(job_id, "job")
        safe_session = (
            validate_identifier(session, "session") if session is not None else None
        )
        if weight == "light":
            return ResourceLease([])
        if weight not in {"heavy", "exclusive"}:
            raise ValueError("weight must be light, heavy, or exclusive")
        requested = self.capacity if weight == "exclusive" else 1
        with self._locked_queue():
            self._prune_stale_tickets()
            self._supersede_queued_session(safe_job, safe_session)
            self._write_ticket(safe_job, requested, safe_session)
        while True:
            if cancelled():
                with self._locked_queue():
                    self._remove_ticket(safe_job)
                raise SchedulerCancelled(f"job cancelled while queued: {safe_job}")
            with self._locked_queue():
                self._prune_stale_tickets()
                tickets = self._ticket_paths()
                if tickets:
                    head = self._read_ticket(tickets[0])
                    if head.get("job_id") == safe_job:
                        lease = self._try_slots(safe_job, requested)
                        if lease is not None:
                            self._remove_ticket(safe_job)
                            return lease
            time.sleep(0.1)

    def _slot_holders(self) -> tuple[list[str], int]:
        holders: list[str] = []
        available = 0
        for path in sorted(self.slots.glob("*.lock"), key=lambda item: item.name):
            stream = path.open("a+b")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.seek(0)
                holder = stream.read().decode(errors="replace").strip()
                if holder:
                    holders.append(holder)
            else:
                available += 1
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        return holders, available

    def snapshot(self, job_id: str | None = None) -> dict[str, object]:
        """Return current capacity, queue, and optional waiter information."""
        with self._locked_queue():
            self._prune_stale_tickets()
            tickets = [self._read_ticket(path) for path in self._ticket_paths()]
            holders, available = self._slot_holders()
        report: dict[str, object] = {
            "capacity": self.capacity,
            "held_permits": self.capacity - available,
            "available_permits": available,
            "queued_jobs": len(tickets),
            "exclusive_waiting": any(
                int(ticket["requested_permits"]) == self.capacity for ticket in tickets
            ),
        }
        if job_id is None:
            return report
        for index, ticket in enumerate(tickets):
            if ticket.get("job_id") != job_id:
                continue
            older = [str(value["job_id"]) for value in tickets[:index]]
            report.update(
                {
                    "queue_position": index + 1,
                    "queue_wait_seconds": max(
                        0.0,
                        time.time() - float(ticket["created_at"]),
                    ),
                    "requested_permits": int(ticket["requested_permits"]),
                    "blocked_by": list(dict.fromkeys([*older, *holders])),
                },
            )
            break
        return report
