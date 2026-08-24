# Architecture

Remote Worktree Runner separates the editing plane from the execution plane.

```text
local Git worktree
  |  push exact commit to a private bare mirror
  |  rsync tracked modifications, deletions, and untracked files
  v
remote detached worktree
  |  systemd --user persistent job
  |  job-private Docker config and Buildx builder
  v
remote Docker / Compose / Kind workload
```

## Local control plane

`trie-run` discovers the repository and active worktree, validates its name, pushes the selected commit to a job-specific ref in a bare remote mirror, prepares a detached server worktree, then overlays the current working state with rsync. Dependency directories, build outputs, Git internals, local environment files, and tool caches are excluded.

The CLI sends the command as a JSON array over SSH. This preserves argument boundaries and avoids reconstructing a shell command in the runner. A user may still choose `bash -lc` explicitly when a product-owned command needs shell semantics.

## Server control plane

`trie-runner` validates repository, job, and role identifiers and resolves every generated path below the configured remote root. The server stores immutable job metadata, status transitions, output logs, cancellation requests, Docker configuration, and workspaces under that root.

Jobs run as transient user systemd units. The job survives a client disconnect, while `status` and `logs -f` reconnect to persisted state. Final states are `passed`, `failed`, and `cancelled`, with the exact process exit code retained.

## Docker isolation

Each job receives:

- a unique Compose project name
- a unique docker-container Buildx builder
- a private `DOCKER_CONFIG`
- job-specific workspaces, shims, logs, and metadata
- repository-scoped language caches

The Docker shim validates commands and records product-supplied Compose project names. Published port checks reject conflicts with another Compose project. Buildx uses `default-load=true` so images built by the isolated builder are available to the server daemon for subsequent runs.

Cleanup brings down only recorded Compose projects, optionally removes their named volumes, removes the builder through its private Docker configuration, and removes the exact job worktrees. Daemon-wide prune and restart commands are prohibited.

## Scheduling and capacity

A filesystem lock admits one heavy job at a time. Default thresholds require 100 GiB free to admit a heavy job, warn below 80 GiB, and request cancellation below 60 GiB. These values are configurable through environment variables.

## Multi-worktree jobs

`--include role=/absolute/local/path` synchronizes another worktree at its exact commit and exposes the remote path as `TRIE_<ROLE>_ROOT`. This supports integration and certification suites spanning multiple repositories without merging their local branches.

## Traefik development gateway

The gateway is a long-lived Compose project beside the transient runner jobs:

```text
Cloudflare Tunnel or server-local HTTP client
  |  http://127.0.0.1:18080
  v
Traefik gateway
  |  exact hostname rules from the file provider
  v
remote-worktree-runner-edge network
  |
  +-- product web service
  +-- product API service
```

Static assets live in the repository. The installer copies them to
`services/gateway` under the selected remote root and preserves
`gateway/dynamic` across reinstalls. Traefik watches that directory, so route
files can be added and removed atomically without restarting the gateway.

Traefik intentionally does not use its Docker provider. Product deployment
logic owns service discovery and writes an explicit hostname-to-service route
only after the target container is available on `remote-worktree-runner-edge`.
The gateway initially has no product route and returns HTTP 404 for unmatched
requests.

### Stable preview registry

The gateway installer stores a private slot configuration that binds each
stable hostname to one repository. The runner registry is the only component
that writes managed preview routes. Each route starts with a compact ownership
record containing the slot, repository, job, Compose project and service,
container ID, internal port, check path, and publication time.

Publication resolves exactly one running Compose container from project and
service labels. A health check, when present, must report healthy. The selected
container joins the external edge network under a unique network alias. The
registry first requests the container directly, atomically replaces the route,
then requests the same hostname through the loopback gateway.

If either check fails, rollback restores the previous route bytes and removes
an unused candidate network attachment. A successful handoff disconnects the
old container only when no other managed slot references it. Route changes are
serialized by a filesystem lock, and cleanup checks the same ownership records
before changing any job Docker resources.
