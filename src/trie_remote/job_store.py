"""Atomic persistent storage for remote job state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from trie_remote.common import ensure_below, validate_identifier
from trie_remote.server_paths import ServerPaths


FINAL_STATES = frozenset({"passed", "failed", "cancelled"})
TRANSITIONS = {
    "preparing": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"running", "cancelled"}),
    "running": FINAL_STATES,
}


def utc_now() -> str:
    """Return a stable UTC timestamp suitable for metadata."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Serializable description of one remote command."""

    job_id: str
    repository: str
    workspace: str
    workspaces: Mapping[str, str]
    includes: Mapping[str, str]
    weight: str
    argv: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        validate_identifier(self.job_id, "job")
        validate_identifier(self.repository, "repository")
        if self.weight not in {"light", "heavy"}:
            raise ValueError("weight must be light or heavy")
        if not self.argv or not all(
            isinstance(value, str) and value for value in self.argv
        ):
            raise ValueError("argv must contain non-empty strings")
        for role in self.workspaces:
            validate_identifier(role, "role")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation."""
        value = asdict(self)
        value["argv"] = list(self.argv)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobSpec":
        """Validate and build a spec received over SSH."""
        return cls(
            job_id=str(value["job_id"]),
            repository=str(value["repository"]),
            workspace=str(value["workspace"]),
            workspaces={str(k): str(v) for k, v in dict(value["workspaces"]).items()},
            includes={
                str(k): str(v) for k, v in dict(value.get("includes", {})).items()
            },
            weight=str(value["weight"]),
            argv=tuple(str(item) for item in value["argv"]),
            created_at=str(value.get("created_at") or utc_now()),
        )


class JobStore:
    """Read and mutate one-job-per-directory persistent state."""

    def __init__(self, paths: ServerPaths) -> None:
        self.paths = paths

    def job_directory(self, job_id: str) -> Path:
        """Return the validated directory for a job."""
        safe = validate_identifier(job_id, "job")
        return ensure_below(self.paths.jobs, self.paths.jobs / safe)

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def create(self, spec: JobSpec) -> Path:
        """Create the immutable job contract and initial state."""
        directory = self.job_directory(spec.job_id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self._write_json(directory / "metadata.json", spec.to_dict())
        self._write_json(directory / "command.json", list(spec.argv))
        self._write_json(
            directory / "status",
            {"state": "preparing", "created_at": spec.created_at},
        )
        output = directory / "output.log"
        output.touch(mode=0o600)
        output.chmod(0o600)
        return directory

    def exists(self, job_id: str) -> bool:
        """Return whether a complete job record has been materialized."""
        directory = self.job_directory(job_id)
        return (directory / "metadata.json").is_file() and (
            directory / "status"
        ).is_file()

    def load(self, job_id: str) -> JobSpec:
        """Load a stored job specification."""
        path = self.job_directory(job_id) / "metadata.json"
        return JobSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def status(self, job_id: str) -> dict[str, Any]:
        """Load current state."""
        path = self.job_directory(job_id) / "status"
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def transition(self, job_id: str, state: str, **details: Any) -> dict[str, Any]:
        """Apply one valid state transition atomically."""
        current = self.status(job_id)
        old_state = str(current["state"])
        if state not in TRANSITIONS.get(old_state, frozenset()):
            raise ValueError(f"invalid job transition: {old_state} -> {state}")
        updated = {**current, **details, "state": state, "updated_at": utc_now()}
        self._write_json(self.job_directory(job_id) / "status", updated)
        return updated

    def finish(self, job_id: str, exit_code: int) -> dict[str, Any]:
        """Record exact process exit and its corresponding final state."""
        state = "passed" if exit_code == 0 else "failed"
        return self.transition(
            job_id, state, exit_code=int(exit_code), finished_at=utc_now()
        )

    def log_path(self, job_id: str) -> Path:
        """Return the validated log path."""
        return self.job_directory(job_id) / "output.log"

    def request_cancel(self, job_id: str) -> Path:
        """Persist a cancellation request observed by a worker."""
        path = self.job_directory(job_id) / "cancel.requested"
        path.touch(mode=0o600)
        path.chmod(0o600)
        return path
