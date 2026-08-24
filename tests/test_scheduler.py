"""Tests for remote runner scheduling and disk protection."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from trie_remote.scheduler import DiskGuard, HeavyJobLease


class SchedulerTests(unittest.TestCase):
    def test_heavy_admission_requires_minimum_free_space(self) -> None:
        guard = DiskGuard(100, 80, 60, lambda _path: 99 * 1024**3)
        with self.assertRaises(RuntimeError):
            guard.admit(Path("/tmp"), "heavy")
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


if __name__ == "__main__":
    unittest.main()
