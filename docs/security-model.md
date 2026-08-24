# Security model

## Trust boundary

Remote Worktree Runner assumes the local operator, selected repository code, remote Unix account, remote host, and Docker daemon are trusted. It is designed to isolate concurrent development jobs and prevent accidental broad cleanup. It does not safely execute hostile repository code.

## Controls

- Repository, job, and role identifiers accept only lowercase letters, digits, and hyphens and are limited to 63 characters.
- Generated server paths are resolved and required to remain strict descendants of the configured remote root.
- The server accepts only repositories in its explicit installer-configured allowlist.
- Job commands cross SSH as JSON argument arrays.
- Job metadata and status are written atomically with private file modes.
- Each job receives a separate Docker configuration, Buildx builder, Compose namespace, worktree, and log.
- Docker daemon restart, system prune, builder-wide prune, and unrelated builder operations are rejected.
- Published ports are checked against existing Compose project ownership.
- Cleanup targets recorded job resources and requires an explicit flag to remove named volumes.
- Toolchain downloads are pinned and verified against publisher checksums or repository-pinned SHA-256 values.
- The optional Traefik gateway publishes only a loopback listener. It runs with
  a read-only root filesystem, all Linux capabilities dropped, and
  `no-new-privileges` enabled.
- The gateway does not mount the Docker socket. It uses a read-only file
  provider, and its API, dashboard, and private health entrypoint are not
  published.
- Cloudflare Tunnel is the intended public HTTP boundary. Exact hostname rules
  should reach the single loopback gateway origin, while SSH remains a separate
  access-controlled route.
- Product databases, caches, message brokers, and the Docker daemon must remain
  private and must not receive public gateway routes.
- Preview slots accept only an installer-approved hostname and repository.
  Publication resolves containers from a job-recorded Compose project and
  never reads container environment variables.
- Managed routes contain a validated ownership record. Cleanup refuses to
  remove a job that owns an active preview, and unpublish requires the current
  owner job ID.
- Only stateless HTTP frontends and APIs should be published. Databases,
  caches, queues, brokers, and other stateful services must remain on private
  product networks.

## Residual risks

- Docker socket access is effectively host-level privilege. A permitted workload can affect the server outside ordinary container boundaries.
- Product commands may intentionally invoke a shell and are responsible for their own quoting and secret handling.
- SSH host verification, Cloudflare Access policy, Unix account hardening, Docker daemon security, backups, and network firewalls are operator responsibilities.
- Shared language and browser caches improve performance but are not suitable across mutually hostile tenants.
- Native `linux/amd64` verification does not prove behavior on ARM64 or another operating system.
- Containers attached to the shared gateway edge network can exchange traffic.
  Only trusted development workloads should join that network.
- The shared edge network is a trust boundary, not tenant isolation. A trusted
  workload on that network can attempt to reach another attached preview.
- A malformed dynamic route can expose the wrong HTTP service through
  Cloudflare Tunnel. Route generation and hostname ownership remain operator
  responsibilities.

Use a dedicated server account and host for untrusted or multi-tenant workloads.
