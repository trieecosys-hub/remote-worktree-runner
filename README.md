# Remote Worktree Runner

Keep source code and Git state on your workstation while Docker-heavy builds and tests run on a remote Linux server over SSH.

Remote Worktree Runner transfers the exact local commit plus tracked modifications, deletions, and untracked files. It creates an isolated remote worktree, Docker configuration, Buildx builder, Compose namespace, cache, log, and persistent systemd user job for each run. Closing the terminal does not stop the job.

## Why

- Recover laptop disk space without moving the source of truth off the workstation.
- Keep the normal editing, review, and version-control workflow on the workstation.
- Run Docker, Compose, Buildx, Kind, integration tests, and E2E suites on native `linux/amd64` infrastructure.
- Reconnect to long-running jobs after SSH or the local terminal disconnects.
- Prevent one job from pruning or overwriting another job's Docker resources.
- Combine multiple local worktrees in one remote certification command.

## Requirements

Client:

- macOS or Linux
- Python 3.10+
- Git, OpenSSH, rsync, and an SSH alias that reaches the server

Server:

- Ubuntu or another systemd-based Linux distribution
- Docker Engine with Compose and Buildx
- A non-root Unix account with access to Docker and user systemd services
- Enough disk for the configured admission thresholds

Cloudflare Access is optional. If used, configure it entirely in the SSH alias, for example:

```sshconfig
Host remote-docker
  HostName ssh.example.com
  User runner
  ProxyCommand cloudflared access ssh --hostname %h
```

## Install

Clone this repository, then preview both installers:

```bash
install/install-local.sh --dry-run
install/install-server.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner \
  --repositories remote-worktree-runner,example-api \
  --dry-run
```

Install the local CLI and the server runner:

```bash
install/install-local.sh
install/install-server.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner \
  --repositories remote-worktree-runner,example-api \
  --max-heavy-jobs 3
```

Docker and Kind hosts also need enough inotify capacity for nested
containerd processes. Preview and apply the persistent host limits before
running integration workloads:

```bash
install/configure-host-kernel.sh --host remote-docker --dry-run
install/configure-host-kernel.sh --host remote-docker
```

The host-kernel installer requests `sudo` on the server, applies the limits
live without restarting Docker, and verifies the effective values. The
configuration persists in `/etc/sysctl.d/99-trie-platform-inotify.conf`.

Server jobs use transient `systemd --user` units. The installer enables and
verifies systemd lingering for the remote runner account so jobs survive the
last SSH session closing. On a host that does not grant non-interactive sudo,
run `sudo loginctl enable-linger <runner-user>` once on the server, then rerun
the installer. `trie-run doctor` reports `systemd_linger: true` only when this
prerequisite is active.

Configure the client shell to match the server installation:

```bash
export REMOTE_RUNNER_SSH_ALIAS=remote-docker
export REMOTE_RUNNER_ROOT=/srv/remote-worktree-runner
export REMOTE_RUNNER_ALLOWED_REPOSITORIES=remote-worktree-runner,example-api
export REMOTE_RUNNER_MAX_HEAVY_JOBS=3
```

Legacy `TRIE_REMOTE_*` variables remain supported. Public `REMOTE_RUNNER_*` names take precedence.

Verify connectivity:

```bash
trie-run doctor --show-sync
```

## Install the development gateway

The optional Traefik gateway gives Cloudflare Tunnel and workstation HTTP
clients one stable origin while product containers continue to use private
Docker networks. Preview the deployment first:

```bash
install/install-gateway.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner \
  --dry-run
```

Install and verify it:

```bash
install/install-gateway.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner \
  --preview-slot api=api.preview.example,example-api

scripts/verify-gateway.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner
```

The gateway binds only to `127.0.0.1:18080`. Its persistent files are stored
under `/srv/remote-worktree-runner/services/gateway`, including the live
`dynamic/` route directory. A healthy gateway starts with no product routes,
so an unmatched request returns HTTP 404. Product deployment tooling can later
write exact-host routes into that directory without restarting Traefik.

Each `--preview-slot SLOT=HOSTNAME,REPOSITORY` argument creates one stable,
server-approved destination. Supplying slots replaces the slot configuration
atomically. Omitting every slot preserves an existing configuration. Slot
hostnames belong in deployment arguments, not tracked files.

For Cloudflare Tunnel, create one HTTP published application origin that points
to `http://127.0.0.1:18080` on the server. Keep SSH as its separate TCP route.
Do not publish product databases, queues, or the Traefik health entrypoint.

## Publish a stable preview

Start a job-owned Compose service without host ports, then publish its internal
HTTP port into a configured slot:

```bash
trie-run preview publish \
  --job api-preview-01 \
  --slot api \
  --project example-api-preview-01 \
  --service web \
  --port 8080 \
  --check-path /health

trie-run preview list
```

Publishing a second verified job to the same slot performs a stable handoff.
The previous route remains available until the candidate passes both a direct
container check and a request through the loopback gateway. To remove a route,
name its current owner:

```bash
trie-run preview unpublish --job api-preview-01 --slot api
trie-run cleanup api-preview-01 --volumes
```

Job cleanup refuses to mutate Docker resources while that job owns an active
preview. Unpublish or hand off the slot first. A disposable two-job handoff can
verify a reserved slot configured for this repository:

```bash
scripts/verify-preview-registry.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner \
  --slot verification
```

## Run and reconnect

Run from the exact worktree being edited:

```bash
trie-run run --job api-check-01 --session api-agent --workload compose -- \
  bash -lc 'docker compose build && docker compose run --rm api-tests'

trie-run status api-check-01
trie-run logs -f api-check-01
trie-run cancel api-check-01
trie-run cleanup api-check-01
```

Job IDs contain lowercase letters, numbers, and hyphens and are at most 63 characters. They are immutable; use a new ID for every run.

For a command that needs another worktree:

```bash
trie-run run --job integration-01 --session integration-agent --workload e2e \
  --include worker=/absolute/path/to/worker-worktree -- \
  bash -lc 'test -d "$TRIE_WORKER_ROOT" && ./scripts/integration.sh'
```

Classify new jobs with `--workload compose`, `browser`, `e2e`, `kind`, or
`certification`. Compose, browser, and E2E jobs default to `heavy`; Kind and
certification default to `exclusive` and reject a weaker explicit weight.
`--session` is optional but recommended for an AI session: a newer queued job
for that same session supersedes its older queued job, without cancelling a
running job or work from another session.

## Safety model

- Heavy jobs enter a FIFO permit pool. `REMOTE_RUNNER_MAX_HEAVY_JOBS` defaults
  to one and may be increased through the server installer's
  `--max-heavy-jobs` option when the host has sufficient capacity.
- An exclusive job acquires the entire pool and cannot be bypassed by newer
  heavy jobs.
- An existing bare Git mirror is read before its shallow-transfer setting is
  changed, so concurrent source reservations do not contend on Git's
  `config.lock`.
- Heavy jobs require 100 GiB free by default. Workers warn below 80 GiB and cancel below 60 GiB.
- Repository names and server paths are validated before use.
- Commands are transported as JSON argument arrays, not reconstructed shell strings.
- Daemon-wide Docker prune and restart operations are rejected.
- Cleanup targets the exact job and preserves named volumes unless `--volumes` is explicit.
- Cleanup refuses a job that still owns an active preview route.
- Playwright browser revisions share a download cache, but jobs disable
  Playwright's automatic stale-browser garbage collection so concurrent jobs
  cannot remove each other's executable. Prune this cache only during a
  maintenance window with no active or queued jobs.
- The optional gateway is loopback-only, has no dashboard, and does not access
  the Docker socket.
- The remote Unix account and Docker daemon remain trusted components.

See [architecture](docs/architecture.md) and [security model](docs/security-model.md) for details.

## Development

```bash
PYTHONPATH=src python3 -u -m unittest discover -s tests -v
bash -n install/*.sh bin/* scripts/*.sh
install/build-zipapp.sh
git diff --check
```

The implementation has no runtime Python dependencies outside the standard library. Downloaded server tools are version-pinned and checksum-verified.

## Compatibility note

The `trie-run`, `trie-runner`, `trie_remote`, and `/srv/trie-platform` defaults are retained for compatibility with the original deployment. They are names, not dependencies on a Trie product. New installations should pass an explicit host, root, and repository allowlist as shown above.

## License

Apache License 2.0. See [LICENSE](LICENSE).
