# Stable Preview Registry Implementation Plan

**Goal:** Add a runner-native registry that safely hands fixed preview
hostnames between verified remote Compose services without exposing host ports
or giving Traefik Docker socket access.

**Architecture:** Pure preview models validate slot configuration and render a
single route file that also carries its ownership record. A server-side
registry resolves job-owned Compose containers, performs direct and gateway
HTTP checks, and atomically replaces or restores the route under a filesystem
lock. Local commands use the existing SSH transport, and cleanup refuses to
remove a job that still owns an active preview.

**Tech stack:** Python 3.10 standard library, Bash, Docker Engine, Docker
Compose, Traefik file provider, SSH, rsync, and `unittest`.

**Spec:** `docs/design/stable-preview-registry.md`

## Global constraints

- Never run Docker-backed checks on workstation Docker Desktop.
- Keep Traefik on the file provider without Docker socket access.
- Publish no additional host ports.
- Accept only configured slot, hostname, and repository mappings.
- Accept only a Compose project recorded for the selected runner job.
- Require one running container and require `healthy` when a health check is
  defined.
- Preserve the previous route until the candidate passes direct and gateway
  HTTP checks.
- Never print or persist container environment variables or credentials.
- Use only reserved hostnames and generic paths in tracked files.
- Do not alter unrelated jobs, containers, networks, builders, or volumes.

---

### Task 1: Add preview models, validation, and route serialization

**Files:**

- Create: `src/trie_remote/preview.py`
- Create: `tests/test_preview.py`

**Interfaces:**

- Produces `PreviewSlot(slot: str, hostname: str, repository: str)`.
- Produces `GatewayRuntime(bind_host: str, bind_port: int, edge_network: str)`.
- Produces `PreviewRoute(slot, hostname, repository, job_id, project, service,
  container_id, network_alias, port, check_path, published_at)`.
- Produces `parse_slot_spec(value: str) -> PreviewSlot`.
- Produces `load_slot_configuration(path: Path) -> dict[str, PreviewSlot]`.
- Produces `write_slot_configuration(path: Path, slots: Iterable[PreviewSlot])`.
- Produces `render_route(route: PreviewRoute) -> str`.
- Produces `parse_route(content: str) -> PreviewRoute`.
- Produces `load_gateway_runtime(path: Path) -> GatewayRuntime`.

- [ ] Write tests for safe slots, hostnames, repositories, services, ports, and
  HTTP paths. Include rejection of uppercase hostnames, wildcards, credentials
  in an authority, control characters, query-only paths, fragments, duplicate
  slots, and duplicate hostnames.

```python
def test_rejects_unsafe_slot_configuration(self) -> None:
    for value in (
        "Bad=app.example.com,example-app",
        "app=*.example.com,example-app",
        "app=https://app.example.com,example-app",
    ):
        with self.subTest(value=value):
            with self.assertRaises(ValueError):
                parse_slot_spec(value)
```

- [ ] Run `PYTHONPATH=src python3 -u -m unittest tests.test_preview -v` and
  confirm it fails because `trie_remote.preview` does not exist.
- [ ] Implement frozen dataclasses and validators. Use the existing
  `validate_identifier` for slot, repository, job, project, and service values.
  Validate DNS labels individually, accept ports `1..65535`, and require paths
  to begin with `/` and contain no scheme, authority, fragment, CR, or LF.

```python
@dataclass(frozen=True, slots=True)
class PreviewSlot:
    slot: str
    hostname: str
    repository: str


@dataclass(frozen=True, slots=True)
class GatewayRuntime:
    bind_host: str
    bind_port: int
    edge_network: str


@dataclass(frozen=True, slots=True)
class PreviewRoute:
    slot: str
    hostname: str
    repository: str
    job_id: str
    project: str
    service: str
    container_id: str
    network_alias: str
    port: int
    check_path: str
    published_at: str
```

- [ ] Implement slot JSON reads and atomic writes with mode `0600`. Reject
  unknown keys and require the JSON top level to be an object keyed by slot.
- [ ] Load `gateway.env` with an exact whitelist of `GATEWAY_BIND_HOST`,
  `GATEWAY_BIND_PORT`, and `GATEWAY_EDGE_NETWORK`. Require loopback bind host,
  validate the port and network identifier, reject duplicate or unknown keys,
  and never return `TRAEFIK_IMAGE` to callers.
- [ ] Implement route rendering with this first-line ownership contract:

```text
# remote-worktree-runner-preview: {"check_path":"/",...}
```

  Render exact Host rules, a `web` entrypoint, and a service URL of
  `http://<network_alias>:<port>`. Parse only that exact prefix, reject unknown
  metadata keys, and revalidate every field.
- [ ] Run the focused tests, then the full unit suite.
- [ ] Commit with `feat: add stable preview route models`.

### Task 2: Implement transactional server-side publication

**Files:**

- Create: `src/trie_remote/preview_registry.py`
- Create: `tests/test_preview_registry.py`

**Interfaces:**

- Consumes the models and serialization functions from Task 1.
- Produces `http_status(address, port, path, host_header, timeout=3.0) -> int`.
- Produces `PreviewRegistry(paths, *, run=subprocess.run,
  status_request=http_status)`.
- Produces `PreviewRegistry.list() -> list[PreviewRoute]`.
- Produces `PreviewRegistry.publish(...) -> PreviewRoute`.
- Produces `PreviewRegistry.unpublish(job_id, slot) -> PreviewRoute`.
- Produces `PreviewRegistry.assert_cleanup_allowed(job_id) -> None`.

- [ ] Write mocked tests for slot/repository ownership, default and recorded
  Compose project ownership, exact project/service container selection, running
  state, optional health state, edge-network attachment, and direct HTTP
  checking.

```python
def test_publish_rejects_unrecorded_compose_project(self) -> None:
    registry = self.registry_for_job(repository="example-process", projects=[])
    with self.assertRaisesRegex(ValueError, "project is not owned by job"):
        registry.publish(
            job_id="preview-01",
            slot="process",
            project="unrelated",
            service="web",
            port=80,
            check_path="/",
        )
```

- [ ] Run the focused registry tests and confirm failure because the registry
  module does not exist.
- [ ] Implement a lock context using `fcntl.flock` on
  `services/gateway/preview.lock`. Resolve routes only below the gateway dynamic
  directory and reject symlinks or malformed preview files.
- [ ] Resolve candidates with Docker label filters for
  `com.docker.compose.project` and `com.docker.compose.service`, then inspect
  only the selected container fields. Never request `.Config.Env`.
- [ ] Connect the candidate to the configured external network using the alias
  `preview-<slot>-<12-character-container-prefix>`. If the exact connection and
  alias already exist, treat it as idempotent.
- [ ] Implement direct HTTP checks with `http.client.HTTPConnection`. Send the
  configured hostname as `Host`, disable redirects, and accept only `200..399`.
- [ ] Write tests for first publish, idempotent republish, successful A-to-B
  handoff, old-container detachment, and retention when another slot references
  the old container.
- [ ] Implement route publication by saving the exact previous bytes, writing a
  mode `0644` temporary file in the dynamic directory, calling `os.replace`,
  then polling the loopback gateway. On failure, restore the previous bytes or
  remove a first-publish route, wait for the prior response, and detach only an
  unused candidate.
- [ ] Write and pass tests for direct-check failure before mutation,
  gateway-check rollback, malformed ownership metadata, and safe diagnostics.
- [ ] Implement unpublish ownership checks and cleanup refusal listing sorted
  slots owned by the job.
- [ ] Make cleanup ownership checks a no-op when the optional gateway or slot
  configuration has never been installed. Publication and unpublication still
  fail when required gateway assets are absent.
- [ ] Run focused tests and the full unit suite.
- [ ] Commit with `feat: add transactional preview registry`.

### Task 3: Expose preview commands and protect cleanup

**Files:**

- Modify: `src/trie_remote/local_cli.py`
- Modify: `src/trie_remote/server_cli.py`
- Modify: `tests/test_local_cli.py`
- Modify: `tests/test_entrypoints.py`
- Modify: `tests/test_job_lifecycle.py`

**Interfaces:**

- Local commands: `preview publish`, `preview list`, and `preview unpublish`.
- Server transport commands: `preview-publish`, `preview-list`, and
  `preview-unpublish`.
- Cleanup calls `PreviewRegistry.assert_cleanup_allowed(job_id)` before any
  Compose or builder mutation.

- [ ] Add parser tests for the exact local command contract and tests that
  verify argument boundaries are preserved through `Transport.ssh`.

```python
arguments = parser.parse_args(
    [
        "preview", "publish", "--job", "preview-01", "--slot", "process",
        "--project", "trie-example-process-preview-01", "--service", "web",
        "--port", "80", "--check-path", "/",
    ],
)
self.assertEqual(arguments.preview_command, "publish")
```

- [ ] Run the local CLI tests and confirm failure because `preview` is not a
  command.
- [ ] Add the nested local parsers. Validate identifiers and port/path values
  locally, send server arguments as an array, and print server JSON without
  reformatting sensitive diagnostic streams.
- [ ] Add server parsers and dispatch to `PreviewRegistry`. Serialize returned
  dataclasses with `dataclasses.asdict` and sorted JSON keys.
- [ ] Add a cleanup lifecycle test proving an owning job raises before the first
  Docker command. Add a non-owner test proving existing cleanup behavior is
  unchanged.
- [ ] Insert the cleanup ownership guard before loading Compose projects or
  running `docker compose down`.
- [ ] Run focused tests and the full unit suite.
- [ ] Commit with `feat: expose stable preview commands`.

### Task 4: Configure slots through the gateway installer

**Files:**

- Modify: `install/install-gateway.sh`
- Modify: `tests/test_gateway_assets.py`

**Interfaces:**

- Adds repeatable `--preview-slot SLOT=HOSTNAME,REPOSITORY`.
- Produces `<remote-root>/services/gateway/preview-slots.json` mode `0600`.
- Preserves an existing slot file when no slot arguments are supplied.

- [ ] Add tests for deterministic dry-run output, multiple sorted slot specs,
  duplicate rejection, invalid hostname/repository rejection, and the absence of
  actual slot configuration from tracked gateway assets.
- [ ] Run the focused installer tests and confirm failure because the argument
  is unknown.
- [ ] Collect slot arguments in Bash, then invoke the Task 1 parser through
  `PYTHONPATH="$repository_root/src" python3` to build a temporary JSON file.
  Do not reconstruct JSON with shell interpolation.
- [ ] Upload the generated file only when at least one slot was supplied. On the
  server, install it atomically with mode `0600`; otherwise preserve the current
  file or create `{}` on a fresh gateway installation.
- [ ] Make dry-run print only sorted `slot -> hostname (repository)` mappings.
  Never print environment values.
- [ ] Run focused tests, the full suite, and `bash -n install/*.sh`.
- [ ] Commit with `feat: configure stable preview slots`.

### Task 5: Add remote verification and public documentation

**Files:**

- Create: `tests/fixtures/preview-service/compose.yaml`
- Create: `scripts/verify-preview-registry.sh`
- Modify: `tests/test_gateway_assets.py`
- Modify: `tests/test_installers.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security-model.md`

**Interfaces:**

- The fixture service uses the already pinned Traefik image, listens on internal
  port `8080`, and returns HTTP 200 from `/ping`.
- The verifier consumes explicit host, remote root, and reserved verification
  slot arguments.

- [ ] Add tests requiring the fixture to have no host ports, requiring verifier
  cleanup to be fixture-scoped, and requiring docs for publish, list,
  unpublish, handoff, and cleanup protection.
- [ ] Run the focused tests and confirm failure before adding the fixture,
  verifier, and documentation.
- [ ] Add a Compose fixture with two project instances created by two unique
  light runner jobs. Use the pinned Traefik image and its `/ping` endpoint; do
  not add a new image dependency.
- [ ] Implement a strict verifier that publishes fixture A, verifies its
  container ID through `preview list`, publishes fixture B into the same slot,
  proves the route changed, proves cleanup of B is refused, unpublishes B, and
  cleans both exact jobs and Compose projects. Trap cleanup must never prune or
  enumerate unrelated resources for deletion.
- [ ] Document slot installation and the three CLI commands using only
  `remote-docker`, `/srv/remote-worktree-runner`, reserved domains, and example
  repositories.
- [ ] Document the route file ownership record, unique aliases, rollback,
  cleanup refusal, shared-edge-network risk, and prohibition on publishing
  stateful infrastructure.
- [ ] Run the full unit suite, shell syntax checks, reproducible archive build,
  `git diff --check`, forbidden-term scan, sensitive-pattern scan, and tracked
  file review.
- [ ] Commit with `docs: document stable preview workflows`.

### Task 6: Deploy and verify the registry on the remote server

**Files:**

- No tracked file changes expected.

**Interfaces:**

- Consumes deployment slot mappings only as runtime installer arguments.
- Produces an updated runner, configured gateway slots, and a passing disposable
  handoff verification.

- [ ] Capture running job units, container IDs, restart counts, gateway health,
  and current dynamic route files without changing them.
- [ ] Re-run the complete local verification suite on the exact branch head.
- [ ] Install the updated server runner with the existing runtime repository
  allowlist. Do not write the deployment host, root, hostnames, or repository
  list into tracked files.
- [ ] Re-run the gateway installer with the four deployment slot mappings and a
  reserved verification slot supplied only on the command line.
- [ ] Run `scripts/verify-preview-registry.sh` against the reserved slot and
  require successful A-to-B handoff, cleanup refusal, unpublish, and exact
  fixture cleanup.
- [ ] Independently inspect the slot file mode, route directory, gateway Docker
  hardening, and absence of fixture resources.
- [ ] Confirm all pre-existing job units and container IDs remain present with
  unchanged restart counts unless they completed through their own workflow.
- [ ] Scan the complete reachable Git history for credentials, tokens, private
  keys, deployment addresses, tunnel identifiers, and forbidden terminology.
- [ ] Merge to `main` only after user approval, push, verify the remote SHA, and
  require the repository CI workflow to succeed.

## Follow-up product plans

This plan delivers the shared registry and proves it with a disposable HTTP
service. Product previews remain separate plans because each repository needs a
different production-like Compose definition and health contract. Implement
them in this order after Task 6 succeeds: Process, Space, Center, then Video.
