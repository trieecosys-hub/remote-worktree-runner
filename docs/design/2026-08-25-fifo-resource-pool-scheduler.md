# FIFO Resource Pool Scheduler

## Status

Approved design for implementation.

## Problem

The runner currently serializes every heavy job through one advisory file
lock. This protects shared Docker resources, but it also lets a long-running,
well-behaved workload block unrelated repositories while the server still has
ample CPU, memory, and disk capacity. Waiting workers remain alive in systemd,
but their persisted job records contain no queue position or blocking reason.

## Goals

- Run up to a configured number of ordinary heavy jobs concurrently.
- Preserve strict FIFO admission so sustained submissions cannot starve an
  older job.
- Provide an exclusive job class for workloads that use server-global
  resources.
- Expose queue position, wait duration, and current blockers through `status`.
- Recover automatically when a waiting or running worker exits unexpectedly.
- Preserve the existing durable job lifecycle and reconnectable commands.

## Non-goals

- Scheduling across multiple servers.
- Automatically splitting one test suite into shards.
- Predicting job duration or implementing priority classes.
- Replacing systemd as the worker lifecycle manager.
- Changing product repositories or their Docker Compose files.

## Resource model

The server owns a fixed pool of heavy permits. The permit count is configured
by `REMOTE_RUNNER_MAX_HEAVY_JOBS` and defaults to `1` for compatibility. The
target server deployment sets it to `3` after verification.

Job weights have these meanings:

- `light`: does not acquire a heavy permit.
- `heavy`: acquires one permit.
- `exclusive`: acquires every permit and therefore runs alone.

Existing `heavy` submissions remain valid. Workloads that create shared Kind
clusters, use fixed non-job-scoped ports, run full-host certification, or
otherwise mutate server-global state must use `exclusive`.

## Durable queue and kernel-backed leases

Scheduler state lives below `<remote-root>/locks/heavy-pool`:

```text
heavy-pool/
  queue.lock
  queue/
    <created-nanoseconds>-<job-id>.json
  slots/
    0.lock
    1.lock
    2.lock
```

Each heavy or exclusive worker creates one private queue ticket. Ticket
creation and removal are serialized by `queue.lock`. Tickets record only the
job identifier, requested permit count, and creation timestamp.

Actual permits are advisory kernel locks on the slot files. A worker retains
the open file descriptors for the duration of the workload. The kernel
therefore releases permits if the process exits, is killed, or its systemd
unit is collected.

## FIFO admission

A waiting worker performs the following loop:

1. Lock `queue.lock`.
2. Remove tickets whose job is final, missing, or whose worker unit is
   confirmed inactive.
3. Sort tickets by creation timestamp and job identifier.
4. If this job is not the oldest ticket, release `queue.lock` and wait.
5. If it requests one permit, try every slot in numeric order until one can be
   locked without blocking.
6. If it requests all permits, acquire every slot without blocking; on a
   partial acquisition, release every acquired slot.
7. On success, remove the ticket, release `queue.lock`, and transition the job
   to `running`.
8. On failure, release `queue.lock` and retry after one second.

Strict FIFO intentionally allows an exclusive job at the head of the queue to
stop later heavy jobs from bypassing it. This prevents exclusive starvation.

## Status visibility

Persisted lifecycle states remain `preparing`, `queued`, `running`, `passed`,
`failed`, and `cancelled`. The server enriches a non-final `status` response
with scheduler data computed from tickets and held slots:

```json
{
  "state": "queued",
  "queue_position": 2,
  "queue_wait_seconds": 48.3,
  "requested_permits": 1,
  "available_permits": 0,
  "blocked_by": ["older-job"]
}
```

These fields are observational and are not written into the lifecycle status
file every second. This avoids unnecessary disk writes and preserves the
atomic state transition contract.

`doctor` reports the configured permit count, held permit count, queued job
count, and exclusive waiter presence.

## Cancellation and failure recovery

- A queued worker checks `cancel.requested` during every wait iteration.
- Cancelling a queued job transitions it to `cancelled`, removes its ticket,
  and does not signal unrelated workers.
- A running worker keeps the existing process-group cancellation behavior.
- Kernel locks release automatically on worker death.
- Every waiter prunes stale tickets before attempting admission.
- Existing inactive-unit reconciliation remains the final fallback for stale
  lifecycle records.

## Deployment

The implementation first deploys with one permit, exercises scheduler tests
and one scoped remote concurrency fixture, then changes the target server to
three permits. Existing workers that use the old single lock must reach a
final state before the pool configuration is activated.

The deployment does not cancel or migrate active product jobs.

## Verification

Automated tests cover:

- no more than the configured number of heavy leases;
- strict FIFO order under concurrent waiters;
- exclusive jobs acquiring all permits;
- heavy jobs waiting behind an older exclusive job;
- cancellation while queued;
- permit recovery after worker termination;
- stale ticket pruning;
- queue fields in `status` and pool fields in `doctor`;
- compatibility with `light` and existing `heavy` job specifications.

The remote fixture submits two isolated heavy sleep jobs and one exclusive
sleep job. It verifies that both heavy jobs overlap, the exclusive job starts
only after both finish, all units become final, and no ticket or permit remains
held.
