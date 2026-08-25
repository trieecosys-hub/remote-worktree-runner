# Runner Throughput Implementation Plan

**Goal:** Replace the global heavy lock with a durable FIFO permit pool, then reduce remote control and source-transfer sessions without changing product repositories.

**Architecture:** The scheduler keeps durable FIFO tickets and kernel-backed slot leases below the runner lock directory. The transfer protocol adds immutable per-role overlay manifests, batched workspace preparation, sparse rsync, and one execute stream while preserving existing commands for compatibility.

**Technology:** Python 3.10 standard library, Git, rsync, OpenSSH, systemd user services, unittest, Bash.

**Specifications:**

- `docs/design/2026-08-25-fifo-resource-pool-scheduler.md`
- `docs/design/2026-08-25-batched-sparse-source-transfer.md`

## Global constraints

- Keep source editing and Git operations local.
- Do not modify TrieVMS, Trie Center, Trie Process, or Trie Space source code.
- Do not cancel or migrate active product jobs during deployment.
- Keep the existing version 1 server commands available during rollout.
- Never transfer ignored credentials, local environment files, dependency caches, or build outputs.
- Keep every server path below the configured remote root.
- Deploy the resource pool with one permit before changing the target server to three.

## Task 1: Extend configuration and job contracts

**Files:**

- Modify: `src/trie_remote/config.py`
- Modify: `src/trie_remote/job_store.py`
- Modify: `tests/test_common.py`
- Modify: `tests/test_job_store.py`

**Interfaces:**

- `RunnerConfig.max_heavy_jobs: int`
- `JobSpec.weight` accepts `light`, `heavy`, or `exclusive`
- `JobSpec.overlays: Mapping[str, OverlayManifest]`
- `OverlayManifest(transfer: tuple[str, ...], delete: tuple[str, ...])`

1. Add tests asserting a default permit count of one, a positive public environment override, rejection of zero or negative values, acceptance of `exclusive`, and exact overlay serialization.
2. Run the focused tests and confirm failures caused by missing fields and validation.
3. Add the minimal configuration and immutable contract types.
4. Run focused tests and the existing job-store/configuration suites.
5. Commit with `feat: extend runner scheduling and overlay contracts`.

## Task 2: Implement the durable FIFO permit pool

**Files:**

- Replace: `src/trie_remote/scheduler.py` heavy lease implementation
- Modify: `tests/test_scheduler.py`

**Interfaces:**

- `ResourcePool(root: Path, capacity: int, store: JobStore | None = None)`
- `ResourcePool.wait(job_id: str, weight: str, cancelled: Callable[[], bool]) -> ResourceLease`
- `ResourcePool.snapshot(job_id: str | None = None) -> dict[str, object]`
- `ResourceLease.release() -> None`

1. Add multiprocessing tests proving two heavy jobs overlap with capacity two, a third waits, an older exclusive waiter prevents bypass, cancellation removes a ticket, and process exit releases slots.
2. Run the scheduler suite and confirm it fails because the pool does not exist.
3. Implement private queue locking, atomic ticket creation, sorted FIFO admission, non-blocking slot locks, exclusive all-slot acquisition, stale-ticket pruning, cancellation, and snapshot reporting.
4. Run scheduler tests repeatedly to detect ordering races.
5. Commit with `feat: add fifo heavy resource pool`.

## Task 3: Integrate scheduler lifecycle and observability

**Files:**

- Modify: `src/trie_remote/job_worker.py`
- Modify: `src/trie_remote/server_cli.py`
- Modify: `tests/test_job_lifecycle.py`
- Modify: `README.md`

**Interfaces:**

- Queued status adds `queue_position`, `queue_wait_seconds`, `requested_permits`, `available_permits`, and `blocked_by`.
- Doctor adds `scheduler_capacity`, `scheduler_held_permits`, `scheduler_queued_jobs`, and `scheduler_exclusive_waiting`.

1. Add lifecycle tests for queued cancellation before builder creation, status enrichment, doctor pool fields, and exclusive disk admission.
2. Run the focused lifecycle tests and confirm the missing behavior.
3. Replace `HeavyJobLease` use with `ResourcePool.wait`, check cancellation while waiting, and create builders only after admission.
4. Enrich read responses without continuously rewriting lifecycle records.
5. Document `REMOTE_RUNNER_MAX_HEAVY_JOBS` and `exclusive` usage.
6. Run scheduler, lifecycle, configuration, and documentation contract tests.
7. Commit with `feat: expose fifo scheduler state`.

## Task 4: Discover and validate sparse overlays

**Files:**

- Modify: `src/trie_remote/repository.py`
- Create: `src/trie_remote/overlay.py`
- Modify: `tests/test_repository.py`
- Create: `tests/test_overlay.py`

**Interfaces:**

- `RepositoryState.overlay(exclude_file: Path) -> OverlayManifest`
- `validate_overlay_path(value: str) -> str`
- `apply_overlay_deletions(workspace: Path, paths: tuple[str, ...]) -> None`

1. Add real Git fixture tests for clean, modified, untracked, deleted, renamed, excluded, unsafe, symlink, and submodule cases.
2. Run the overlay tests and confirm failures from missing discovery and validation.
3. Parse NUL-delimited Git output, apply the existing filter policy, normalize rename pairs, and reject unsupported submodule overlays.
4. Implement containment-checked deletion that never follows a path outside the workspace.
5. Run overlay, repository, and common validation tests.
6. Commit with `feat: add sparse worktree overlay manifests`.

## Task 5: Add protocol version 2 reservation and batched preparation

**Files:**

- Modify: `src/trie_remote/server_cli.py`
- Modify: `src/trie_remote/server_workspace.py`
- Modify: `tests/test_job_lifecycle.py`
- Modify: `tests/test_server_workspace.py`

**Interfaces:**

- `reserve` returns `protocol_version`, `mirrors`, and `workspaces`.
- New server command: `prepare-all --job <job-id>`.

1. Add tests asserting reserve creates all mirrors and returns deterministic descriptions, prepare-all prepares all roles and applies only reserved deletions, and repeated preparation is idempotent.
2. Run focused tests and confirm failures from version 1 output and missing command.
3. Extend reserve response while preserving existing fields; implement batch preparation from the immutable stored specification.
4. Run server workspace, lifecycle, and backward-compatibility tests.
5. Commit with `feat: batch remote workspace preparation`.

## Task 6: Add sparse rsync and the execute stream

**Files:**

- Modify: `src/trie_remote/transport.py`
- Modify: `src/trie_remote/server_cli.py`
- Modify: `src/trie_remote/local_cli.py`
- Modify: `tests/test_transport.py`
- Modify: `tests/test_local_cli.py`
- Modify: `tests/test_job_lifecycle.py`

**Interfaces:**

- `Transport.reserve(spec: JobSpec) -> Reservation`
- `Transport.prepare_all(job_id: str) -> Mapping[str, str]`
- `Transport.sync_overlay(state, workspace, manifest) -> None`
- `Transport.execute(spec: JobSpec) -> subprocess.CompletedProcess`
- New server command: `execute`.

1. Add transport tests proving clean runs skip rsync, dirty runs use NUL-delimited `--files-from`, all roles use one prepare call, and session counts match four clean or five dirty primary-only sessions.
2. Add lifecycle tests proving execute streams log bytes to stderr, emits one final JSON status on stdout, and leaves the worker reconnectable if the client disconnects.
3. Run focused tests and confirm missing methods and commands.
4. Implement protocol negotiation, sparse rsync input, one prepare-all call, and execute framing.
5. Keep the existing version 1 workflow as fallback when `protocol_version` is absent or less than two.
6. Run transport, local CLI, lifecycle, and overlay suites.
7. Commit with `feat: reduce remote execution sessions`.

## Task 7: Verify, deploy, and measure

**Files:**

- Modify: `install/install-server.sh`
- Modify: `scripts/verify-deployment.sh`
- Modify: `tests/test_installers.py`
- Modify: `README.md`

**Interfaces:**

- Server wrapper exports `REMOTE_RUNNER_MAX_HEAVY_JOBS` from installer option `--max-heavy-jobs`.
- Deployment verifier uses job-scoped fixtures and performs no global Docker prune.

1. Add installer tests for a positive `--max-heavy-jobs` value, dry-run output, wrapper configuration, and unsafe input rejection.
2. Add verifier contract tests for two overlapping heavy jobs followed by one exclusive job and exact cleanup.
3. Implement installer and verifier changes, initially using one permit.
4. Run all unit tests, shell syntax checks, whitespace checks, and the sensitive-literal scan.
5. Wait for all pre-pool product jobs to become final without cancelling them.
6. Deploy the runner with one permit and run doctor plus the scoped concurrency fixture.
7. Deploy with three permits, repeat doctor and concurrency verification, then compare clean and dirty session counts and elapsed setup time against the recorded baseline.
8. Confirm no non-final fixture jobs, tickets, held permits, builders, containers, or workspaces remain.
9. Push the reviewed commits to `main` and notify active product tasks of the new `exclusive` classification and queue fields.
