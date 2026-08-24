# Stable Preview Registry Design

## Purpose

The stable preview registry gives each product one durable public hostname while
builds and tests continue to run as isolated remote jobs. A preview hostname is
updated only after a selected Compose service passes direct and gateway-level
HTTP checks. Failed candidates leave the previously working preview unchanged.

The registry extends the existing file-provider Traefik gateway. It does not
enable Traefik's Docker provider, mount the Docker socket into Traefik, or
publish additional host ports.

## Goals

- Provide fixed preview slots such as `video`, `center`, `process`, and `space`.
- Restrict each slot to one configured hostname and repository.
- Publish only a service owned by a recorded remote runner job.
- Keep the previous preview active until its replacement is verified.
- Support atomic handoff, explicit unpublish, safe inspection, and cleanup
  protection.
- Keep deployment hostnames and other environment-specific values outside Git.
- Avoid reading or persisting container environment variables or credentials.

## Non-goals

- General-purpose public ingress for arbitrary containers.
- Automatic publication of every build, test, or certification job.
- Public access to databases, caches, brokers, storage administration ports, or
  the Docker daemon.
- Production deployment orchestration, traffic splitting, or zero-downtime
  database migration.
- Service discovery through Traefik Docker labels.

## Architecture

The registry is implemented inside the existing local and server runner CLIs.
It uses four existing trust anchors:

1. Job metadata identifies the repository that supplied the source.
2. `compose-projects.json` records Compose projects used by that job.
3. Docker Compose labels identify the exact service container.
4. The gateway file-provider directory is the only route activation boundary.

The local CLI sends validated preview requests over the existing SSH transport.
The server resolves the target container, connects it to the gateway edge
network with a unique alias, verifies it, and atomically replaces one route
file. Traefik continues to run without Docker socket access.

## Slot configuration

Preview slots are configured during gateway installation with repeatable,
deployment-only arguments:

```bash
install/install-gateway.sh \
  --host remote-docker \
  --remote-root /srv/remote-worktree-runner \
  --preview-slot video=video.example.com,example-video \
  --preview-slot process=process.example.com,example-process
```

Each value contains:

- a lowercase slot identifier;
- one exact lowercase DNS hostname;
- one allowed repository identifier.

The installer validates the complete set, rejects duplicate slots and
hostnames, and writes `services/gateway/preview-slots.json` with mode `0600`.
When no `--preview-slot` argument is supplied, an existing configuration is
preserved. A fresh installation without slot arguments creates an empty
configuration. Dry-run output contains only public image and routing metadata.

The slot configuration is never generated from repository files. Examples in
documentation use reserved domains only.

## User interface

The local CLI adds one command group:

```text
trie-run preview publish
trie-run preview list
trie-run preview unpublish
```

Publishing uses this contract:

```bash
trie-run preview publish \
  --job process-preview-01 \
  --slot process \
  --project trie-example-process-process-preview-01 \
  --service web \
  --port 80 \
  --check-path /
```

`--job`, `--slot`, `--project`, `--service`, `--port`, and `--check-path` are
required. The path must be an absolute HTTP path without control characters,
fragments, or an authority component. A successful check must return HTTP
status `200` through `399`.

`preview list` returns sorted JSON records containing only:

- slot and hostname;
- owning job and repository;
- Compose project and service;
- container ID prefix and internal port;
- check path and publication timestamp.

Unpublishing requires both the slot and its current owning job:

```bash
trie-run preview unpublish --job process-preview-01 --slot process
```

An ownership mismatch is an error and leaves the route unchanged.

## Route file as the ownership record

Each active slot has one file:

```text
services/gateway/dynamic/preview-<slot>.yaml
```

The first line is a YAML comment containing a compact JSON ownership record.
The remainder is the Traefik HTTP router and service definition. The JSON
contains only validated, non-secret fields listed by `preview list`.

Using the route file as the single source of truth avoids a two-file atomicity
problem between routing state and ownership state. An atomic replacement makes
both the route and its ownership visible together. Unknown, malformed, or
manually edited preview files fail closed and cannot be overwritten or removed
without operator repair.

Route names, service names, filenames, and network aliases are derived only
from validated slot identifiers. Host rules always use the exact configured
hostname. Paths are not used for routing.

## Publish transaction

The server performs publication under an exclusive gateway preview lock:

1. Load and validate the slot configuration.
2. Load job metadata and require the job repository to match the slot.
3. Require the requested Compose project to be the job's default project or an
   entry in its recorded `compose-projects.json`.
4. Resolve exactly one container with matching Compose project and service
   labels.
5. Require the container to be running. If it defines a Docker health check,
   require the state to be `healthy`.
6. Validate that the requested internal TCP port is in the container's valid
   port range.
7. Connect the candidate to the configured gateway edge network with an alias
   containing the slot and a container ID prefix. Reusing an existing matching
   connection is idempotent.
8. Send an HTTP request directly to the candidate edge-network address and port
   with the configured hostname as the Host header.
9. Save the existing route bytes, then atomically replace the slot route with a
   candidate route.
10. Poll the loopback gateway with the same Host header and check path until it
    returns HTTP `200` through `399`.
11. If the gateway check succeeds, the handoff is committed. Disconnect the
    previous container from the edge network only when no other active preview
    references it.
12. If any candidate or gateway check fails, restore the exact previous route
    bytes, wait for the previous route to recover, disconnect the candidate
    when unused, and return a non-zero result.

The unique alias prevents Docker DNS from balancing between the old and new
containers during handoff. Re-publishing the current container with the same
parameters is idempotent.

## Unpublish and cleanup

Unpublish acquires the same lock, validates the ownership comment, atomically
removes the route, waits for the hostname to return HTTP `404`, and disconnects
the old container when no other preview references it.

Runner cleanup checks active preview ownership before changing any Compose
project. If the job owns one or more previews, cleanup fails with the slot names
and instructs the caller to publish a replacement or explicitly unpublish.
Cancellation and normal job completion do not remove previews. This allows a
successful detached Compose deployment to remain available until an explicit
handoff or cleanup.

## Failure handling

- Missing gateway assets or slot configuration fail before Docker mutation.
- An unknown job, repository mismatch, unrecorded project, missing service,
  multiple matching containers, stopped container, or unhealthy container fails
  before route mutation.
- Direct HTTP failure disconnects only the unused candidate connection.
- Gateway HTTP failure restores the previous route before returning.
- Malformed ownership metadata fails closed.
- Diagnostics are scoped to the selected container and gateway Compose service.
  They do not print environment variables, slot configuration contents, or
  Docker inspection output that could contain credentials.
- The operation never prunes Docker resources, restarts the Docker daemon, or
  changes unrelated Compose projects.

## Security properties

- Slot hostname and repository ownership are fixed by server-side
  configuration.
- Arbitrary hostnames cannot be supplied by a publish request.
- A job cannot publish a container from another repository or unrecorded
  Compose project.
- The gateway still has a read-only filesystem, dropped capabilities, no API or
  dashboard, and no Docker socket.
- Only the selected HTTP container joins the shared edge network. Databases,
  caches, brokers, and storage services remain on product-private networks.
- Host ports are unnecessary for preview traffic.
- Route metadata contains no credentials or container environment values.

## Testing

Local tests use the standard library and mocked Docker command results. They
cover:

- slot, hostname, repository, service, port, and path validation;
- exact job, project, and service ownership;
- missing, multiple, stopped, and unhealthy containers;
- route metadata parsing and fail-closed behavior;
- atomic first publish and idempotent republish;
- successful replacement and old-container detachment;
- direct-check failure before route mutation;
- gateway-check failure with exact rollback;
- unpublish ownership checks;
- cleanup refusal for an active preview owner;
- local and server CLI argument transport;
- installer preservation and dry-run behavior.

Remote verification uses a disposable Compose fixture controlled by one scoped
runner job. It publishes fixture A, hands the slot to fixture B, verifies the
old container is no longer reachable from the edge network, validates cleanup
protection, unpublishes, and cleans only fixture resources. Docker-backed tests
run on the remote server, never on workstation Docker Desktop.

## Product rollout

The registry and product preview definitions are separate deliverables because
the products have different runtime contracts. Rollout order is:

1. Process, using its existing web service and same-origin API proxy.
2. Space, using its administrative web entrypoint after the complete service
   stack is healthy.
3. Center, after adding a production-like frontend and backend preview Compose
   definition.
4. Video, using its HTTP application service and version health endpoint.

Each product rollout adds only the product-owned preview definition and tests.
The public hostname is handed over only after the real product endpoint passes
the registry checks. Certification and integration-test projects are never
published automatically.

## Public repository hygiene

Source, tests, and documentation contain only reserved example hostnames and
generic server paths. Deployment slot configuration, runtime route files,
verification output, credentials, addresses, and tunnel identifiers remain
untracked. Before publication, the complete reachable Git history is scanned
for private keys, credentials, access tokens, infrastructure addresses, and
environment-specific hostnames.
