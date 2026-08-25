"""Tests for remote runner scheduling and disk protection."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from trie_remote.scheduler import (
    DiskGuard,
    HeavyJobLease,
    ResourcePool,
    SchedulerCancelled,
)


class SchedulerTests(unittest.TestCase):
    def wait_until(self, predicate: object, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not satisfied")

    def test_heavy_admission_requires_minimum_free_space(self) -> None:
        guard = DiskGuard(100, 80, 60, lambda _path: 99 * 1024**3)
        for weight in ("heavy", "exclusive"):
            with self.subTest(weight=weight), self.assertRaises(RuntimeError):
                guard.admit(Path("/tmp"), weight)
        guard.admit(Path("/tmp"), "light")

    def test_monitor_levels_warn_then_cancel(self) -> None:
        values = iter((79 * 1024**3, 59 * 1024**3))
        guard = DiskGuard(100, 80, 60, lambda _path: next(values))
        self.assertEqual(guard.monitor(Path("/tmp")), "warning")
        self.assertEqual(guard.monitor(Path("/tmp")), "cancel")

    def test_only_one_heavy_lease_can_be_held(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "heavy.lock"
            first = HeavyJobLease(path, "alpha")
            second = HeavyJobLease(path, "beta")
            first.acquire(blocking=False)
            with self.assertRaises(BlockingIOError):
                second.acquire(blocking=False)
            first.release()
            second.acquire(blocking=False)
            second.release()

    def test_pool_allows_capacity_and_queues_the_next_heavy_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = ResourcePool(Path(temporary), 2)
            first = pool.wait("first", "heavy", lambda: False)
            second = pool.wait("second", "heavy", lambda: False)
            acquired: list[object] = []

            thread = threading.Thread(
                target=lambda: acquired.append(
                    pool.wait("third", "heavy", lambda: False),
                ),
            )
            thread.start()
            self.wait_until(lambda: pool.snapshot("third").get("queue_position") == 1)

            self.assertEqual(pool.snapshot()["held_permits"], 2)
            self.assertEqual(acquired, [])
            first.release()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(acquired), 1)

            acquired[0].release()
            second.release()

    def test_older_exclusive_waiter_prevents_heavy_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = ResourcePool(Path(temporary), 2)
            running = pool.wait("running", "heavy", lambda: False)
            order: list[str] = []
            release_exclusive = threading.Event()

            def acquire_exclusive() -> None:
                lease = pool.wait("exclusive", "exclusive", lambda: False)
                order.append("exclusive")
                release_exclusive.wait(timeout=3)
                lease.release()

            def acquire_heavy() -> None:
                lease = pool.wait("later", "heavy", lambda: False)
                order.append("later")
                lease.release()

            exclusive = threading.Thread(target=acquire_exclusive)
            later = threading.Thread(target=acquire_heavy)
            exclusive.start()
            self.wait_until(
                lambda: pool.snapshot("exclusive").get("queue_position") == 1,
            )
            later.start()
            self.wait_until(lambda: pool.snapshot("later").get("queue_position") == 2)

            self.assertEqual(order, [])
            running.release()
            self.wait_until(lambda: order == ["exclusive"])
            self.assertTrue(later.is_alive())
            release_exclusive.set()
            exclusive.join(timeout=3)
            later.join(timeout=3)
            self.assertEqual(order, ["exclusive", "later"])

    def test_cancelled_waiter_removes_its_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = ResourcePool(Path(temporary), 1)
            running = pool.wait("running", "heavy", lambda: False)
            cancelled = threading.Event()
            errors: list[type[BaseException]] = []

            def wait() -> None:
                try:
                    pool.wait("cancelled", "heavy", cancelled.is_set)
                except BaseException as error:  # noqa: BLE001
                    errors.append(type(error))

            thread = threading.Thread(target=wait)
            thread.start()
            self.wait_until(
                lambda: pool.snapshot("cancelled").get("queue_position") == 1,
            )
            cancelled.set()
            thread.join(timeout=3)

            self.assertEqual(errors, [SchedulerCancelled])
            self.assertEqual(pool.snapshot()["queued_jobs"], 0)
            running.release()

    def test_kernel_releases_permit_when_worker_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = """
import sys
from pathlib import Path
from trie_remote.scheduler import ResourcePool
lease = ResourcePool(Path(sys.argv[1]), 1).wait("child", "heavy", lambda: False)
print("acquired", flush=True)
sys.stdin.read()
"""
            environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            }
            process = subprocess.Popen(
                [sys.executable, "-c", script, temporary],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(process.stdout.readline().strip(), "acquired")
            process.terminate()
            process.wait(timeout=3)
            process.stdin.close()
            process.stdout.close()

            lease = ResourcePool(Path(temporary), 1).wait(
                "parent",
                "heavy",
                lambda: False,
            )
            lease.release()


if __name__ == "__main__":
    unittest.main()
