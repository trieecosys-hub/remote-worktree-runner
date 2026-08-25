"""Heavy-job locking and disk admission policy."""

from __future__ import annotations

from collections.abc import Callable
import fcntl
import os
from pathlib import Path
import shutil
from typing import BinaryIO


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

    def __enter__(self) -> "HeavyJobLease":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
