"""Tests for persistent remote job metadata."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from trie_remote.job_store import JobSpec, JobStore, OverlayManifest
from trie_remote.server_paths import ServerPaths


class JobStoreTests(unittest.TestCase):
    def test_job_spec_round_trips_exclusive_weight_and_overlays(self) -> None:
        spec = JobSpec(
            job_id="exclusive-job",
            repository="trie-space",
            workspace="/srv/trie-platform/workspaces/trie-space/exclusive-job/primary",
            workspaces={
                "primary": "/srv/trie-platform/workspaces/trie-space/exclusive-job/primary",
            },
            includes={},
            weight="exclusive",
            argv=("true",),
            created_at="2026-08-25T00:00:00+00:00",
            commits={"primary": "a" * 40},
            overlays={
                "primary": OverlayManifest(
                    transfer=("src/changed.py",),
                    delete=("src/removed.py",),
                ),
            },
        )

        self.assertEqual(JobSpec.from_dict(spec.to_dict()), spec)
        self.assertEqual(spec.to_dict()["commits"], {"primary": "a" * 40})
        self.assertEqual(
            spec.to_dict()["overlays"]["primary"],
            {"transfer": ["src/changed.py"], "delete": ["src/removed.py"]},
        )

    def test_job_spec_rejects_unknown_weight(self) -> None:
        with self.assertRaisesRegex(ValueError, "weight must be"):
            JobSpec(
                job_id="bad-weight",
                repository="trie-space",
                workspace="/tmp/workspace",
                workspaces={"primary": "/tmp/workspace"},
                includes={},
                weight="large",
                argv=("true",),
                created_at="2026-08-25T00:00:00+00:00",
            )
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ServerPaths.from_root(Path(self.temporary.name) / "root")
        self.paths.create()
        self.store = JobStore(self.paths)
        self.spec = JobSpec(
            job_id="alpha",
            repository="trie-space",
            workspace="/srv/trie-platform/workspaces/trie-space/alpha/primary",
            workspaces={"primary": "/srv/trie-platform/workspaces/trie-space/alpha/primary"},
            includes={},
            weight="heavy",
            argv=("bash", "-lc", "printf ok"),
            created_at="2026-08-23T00:00:00+00:00",
        )

    def test_create_writes_private_atomic_contract_files(self) -> None:
        self.store.create(self.spec)
        job_dir = self.paths.jobs / "alpha"
        for name in ("metadata.json", "command.json", "status", "output.log"):
            path = job_dir / name
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads((job_dir / "command.json").read_text()),
            ["bash", "-lc", "printf ok"],
        )
        self.assertEqual(self.store.status("alpha")["state"], "preparing")

    def test_rejects_invalid_and_second_final_transitions(self) -> None:
        self.store.create(self.spec)
        with self.assertRaises(ValueError):
            self.store.transition("alpha", "running")
        self.store.transition("alpha", "queued")
        self.store.transition("alpha", "running")
        self.store.finish("alpha", 0)
        with self.assertRaises(ValueError):
            self.store.finish("alpha", 1)


if __name__ == "__main__":
    unittest.main()
