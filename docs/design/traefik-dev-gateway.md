# Traefik Development Gateway Design

## Summary

Remote Worktree Runner will provide an optional, long-lived Traefik gateway on
the remote Docker host. Cloudflare Tunnel or another external ingress can send
HTTP traffic to one loopback-only server port, while Traefik routes requests to
explicitly registered remote job endpoints.

This design covers the gateway foundation only: the Compose stack, secure
installation, lifecycle commands, validation, and documentation. Runner-managed
endpoint registration is a follow-up subsystem so that the gateway can be
deployed and audited independently before jobs are allowed to publish routes.

## Goals

- Run one persistent gateway outside all ephemeral job lifecycles.
- Listen only on a configurable loopback address, defaulting to
  `127.0.0.1:18080`.
- Use Traefik's file provider so the gateway never receives the Docker socket.
- Give future runner commands a small, atomic dynamic-configuration interface.
- Survive server reboots and remain unaffected by `trie-run cleanup`.
- Keep the public repository free of deployment credentials and private
  infrastructure details.
- Provide an idempotent installer and read-only verification command.

## Non-goals

- This change does not create Cloudflare DNS records, Access applications,
  service tokens, or tunnel routes.
- This change does not publish a product container or database.
- This change does not add endpoint discovery, `trie-run expose`, or artifact
  retrieval. Those belong to the next runner-integration design.
- This change does not require edits to product repositories or their Compose
  files.
- This change does not terminate public TLS. Cloudflare remains responsible for
  client-facing HTTPS when Cloudflare Tunnel is used.

## Selected Approach

Use an official Traefik container in a dedicated Docker Compose project. The
gateway uses the file provider rather than the Docker provider. It watches a
directory of generated dynamic configuration files and joins one external edge
network created by the installer.

Nginx was rejected because every route update requires configuration rendering
and a reload. Caddy was rejected because container discovery normally requires
an additional plugin or API-controlled mutable state. Traefik's file provider
supports atomic route updates without a reload or Docker socket access.

## Architecture

```text
workstation browser or agent
          |
          | HTTPS :443
          v
external ingress (for example, Cloudflare Tunnel + Access)
          |
          | HTTP to 127.0.0.1:18080
          v
long-lived Traefik gateway
          |
          | generated file-provider route
          v
explicitly registered container on the shared edge network
```

The Compose project name defaults to `remote-worktree-runner-gateway`. The
gateway container and external edge network use configurable generic names. No
product or customer-specific name is required by the implementation.

The gateway publishes only its HTTP entrypoint to the server loopback address.
The Traefik API and dashboard remain disabled. The internal health entrypoint is
not published to the host.

## Repository Layout

The implementation will add the following focused assets:

```text
gateway/
  compose.yaml
  traefik-static.yaml
  dynamic.example/
    route.yaml
install/
  install-gateway.sh
scripts/
  verify-gateway.sh
tests/
  test_gateway_assets.py
```

`config/versions.env` will hold the pinned Traefik image reference. The
reference must include an immutable digest; floating tags such as `latest` are
not accepted.

The example dynamic route uses documentation-only hostnames and unreachable
example targets. It is never copied into the active server configuration.

## Server Layout and Lifecycle

The installer places immutable gateway assets below a configurable runner root:

```text
<remote-root>/services/gateway/
  compose.yaml
  traefik-static.yaml
  dynamic/
```

The `dynamic` directory is persistent server state and is not sourced from a
product worktree. It starts empty. Future endpoint registration writes one
validated file per job using a temporary file plus atomic rename.

The installer performs these operations:

1. Validate the SSH target, remote root, bind address, image digest, and names.
2. Verify that the chosen host port is free or already owned by this gateway.
3. Create the external edge network if it does not exist.
4. Stage public gateway assets in a temporary server directory.
5. Render and validate the effective Compose configuration.
6. Replace the installed assets atomically.
7. Pull the pinned image and run `docker compose up -d --wait`.
8. Run gateway verification and report the effective loopback endpoint.

Re-running the installer with the same inputs is safe. A failed health check
prints scoped Compose logs and returns a non-zero exit status. The installer
does not prune Docker resources, restart Docker, or touch runner jobs.

The Compose service uses `restart: unless-stopped`, so Docker restores it after
a server reboot. Job cleanup does not know the gateway project name and cannot
remove its container, network, or state.

## Container Hardening

The Traefik service will:

- bind the HTTP entrypoint only to the configured loopback address;
- use a read-only root filesystem;
- mount static configuration and the dynamic directory read-only;
- use a temporary filesystem for writable runtime paths;
- drop all Linux capabilities;
- set `no-new-privileges`;
- disable anonymous usage reporting, version checks, the API, and the dashboard;
- emit access and service logs to standard output without request headers;
- expose no Docker socket and contain no daemon credentials;
- include a container health check against Traefik's internal ping endpoint.

The shared edge network is not sufficient authorization by itself. A future
runner command must both connect a selected product container to that network
and create an explicit route. Containers are never routed merely because they
exist.

## Configuration Contract

Public, non-secret settings are accepted as installer flags or environment
variables:

- SSH alias
- remote root
- loopback bind host and port
- Compose project name
- edge network name
- pinned Traefik image reference

No Cloudflare account ID, zone ID, tunnel token, Access client ID, Access client
secret, SSH password, server IP address, personal username, or production
hostname belongs in these files.

Deployment-specific non-secret values may be written to a server-side file with
mode `0600`. Secrets remain in the external system that consumes them, such as
Cloudflare, a workstation keychain, or an operator-provided secret store. The
gateway itself needs no secret for the chosen architecture.

## Public Repository Hygiene

The implementation will extend ignore and verification coverage for:

- `.env` and local override files;
- private keys and common credential exports;
- generated active dynamic routes;
- deployment transcripts and logs;
- service-token header names paired with literal values;
- tunnel installation tokens and credential JSON files.

Tracked examples use placeholders and reserved domains only. Tests will scan
tracked content for known private deployment values supplied through the test
environment and for credential-shaped patterns. Installation output must not
print environment values, tokens, or complete command lines containing secrets.

Secret scanning is a release gate, but it does not replace Git review. Before a
push, the operator must inspect the staged diff and the tracked-file list.

## Dynamic Route Boundary

The gateway consumes Traefik dynamic YAML files from a single directory. The
future runner integration owns their schema and lifecycle. At minimum, a route
will contain:

- a validated route identifier derived from repository and job IDs;
- an exact hostname, never an unbounded wildcard;
- one HTTP router attached to the public entrypoint;
- one load-balancer service targeting a unique edge-network alias and validated
  container port;
- optional WebSocket-compatible forwarding, which requires no separate public
  port.

The gateway foundation does not accept arbitrary user-supplied Traefik YAML.
Only the later runner registry will be permitted to write active routes.

## Error Handling

- A non-loopback bind address is rejected unless an explicit unsafe override is
  added in a future design.
- An occupied port owned by another process aborts installation before Compose
  changes are applied.
- A missing Docker Engine, Compose plugin, or required network capability aborts
  with an actionable error.
- Invalid YAML or an unpinned image reference fails local/static validation.
- A container that does not become healthy causes installation to fail and
  prints only gateway-scoped diagnostic output.
- Verification distinguishes an absent gateway, an unhealthy container, a
  missing network, and a non-loopback listener.

## Verification and Tests

All Docker-backed verification runs on the remote server, never on workstation
Docker Desktop.

Static tests will verify:

- the local port mapping is loopback-only;
- no Docker socket is mounted;
- the root filesystem is read-only and capabilities are dropped;
- the dashboard and API are disabled;
- the dynamic configuration mount is read-only;
- the image reference is immutable;
- public examples contain no deployment-specific values;
- installer dry-run output is deterministic and secret-safe.

Remote verification will verify:

- the Compose configuration renders successfully;
- the gateway container is healthy;
- the external edge network exists;
- the configured port listens on loopback and not on wildcard interfaces;
- the internal ping check succeeds;
- no application route exists before explicit registration;
- a temporary documentation-only route can be added atomically, exercised from
  the server, and removed without restarting Traefik.

The existing Python unit suite, shell syntax checks, archive build, diff checks,
and tracked-content secret scan remain required before commit or push.

## Deployment Sequence

1. Merge and publish the gateway foundation.
2. Install and verify it on the current remote Docker server.
3. Configure external ingress hostnames to point to the loopback gateway.
4. Design and implement runner-managed endpoint registration and cleanup.
5. Expose the first product service through an exact hostname and verify browser,
   REST, WebSocket, Access-user, and Access-service-token flows.

Until step 4 is complete, the gateway is intentionally healthy but has no
product routes. External ingress will therefore receive a controlled not-found
response rather than reach an unintended container.
