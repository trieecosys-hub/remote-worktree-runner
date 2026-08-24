# Traefik Development Gateway Implementation Plan

**Goal:** Add and deploy a hardened, long-lived Traefik gateway that accepts
HTTP traffic only on `127.0.0.1:18080` and watches an empty dynamic-route
directory without access to the Docker socket.

**Architecture:** A dedicated Docker Compose project runs Traefik on the remote
Docker host. Static configuration enables one HTTP entrypoint, one internal
health entrypoint, and the file provider. Installation and verification scripts
operate over an existing SSH alias and keep deployment-specific values outside
Git.

**Tech stack:** Bash, Docker Engine, Docker Compose, Traefik v3.7.11, Python
`unittest`, SSH, rsync.

**Spec:** `docs/design/traefik-dev-gateway.md`

## Global constraints

- Never run Docker-backed checks on workstation Docker Desktop.
- Publish only `127.0.0.1:18080`; reject non-loopback bind hosts.
- Use `traefik:v3.7.11@sha256:5203c3f39ca70de6790d964624e042463ffbd57715bc82be155cf224c0dd5144`.
- Do not mount `/var/run/docker.sock` or enable the Traefik API/dashboard.
- Do not commit hostnames, IP addresses, usernames, passwords, tokens, keys, or
  deployment output.
- Preserve the remote dynamic-route directory across reinstalls.
- Do not prune Docker resources or alter any runner job.

---

### Task 1: Define and statically validate the gateway assets

**Files:**

- Create: `gateway/compose.yaml`
- Create: `gateway/traefik-static.yaml`
- Create: `gateway/dynamic.example/route.yaml`
- Modify: `config/versions.env`
- Create: `tests/test_gateway_assets.py`
- Modify: `.gitignore`

**Interfaces:**

- Consumes: Compose environment variables `TRAEFIK_IMAGE`,
  `GATEWAY_BIND_HOST`, `GATEWAY_BIND_PORT`, and `GATEWAY_EDGE_NETWORK`.
- Produces: a Compose service named `traefik`, an external edge network, and a
  file-provider directory mounted at `/etc/traefik/dynamic`.

- [ ] Write tests that assert the image reference contains the exact immutable
  digest, the host binding defaults to `127.0.0.1:18080`, the service has a
  read-only root filesystem, all capabilities are dropped, and no Docker socket
  string exists in gateway assets.
- [ ] Run `PYTHONPATH=src python3 -u -m unittest tests.test_gateway_assets -v`
  and confirm it fails because the gateway assets do not exist.
- [ ] Add `TRAEFIK_IMAGE` to `config/versions.env` using the exact version and
  digest from the global constraints.
- [ ] Create `gateway/compose.yaml` with `restart: unless-stopped`,
  `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, a `/tmp` tmpfs,
  loopback-only port interpolation, read-only static/dynamic mounts, an external
  edge network, and a Traefik CLI health check.
- [ ] Create `gateway/traefik-static.yaml` with `web` on `:8080`, `health` on
  `:8082`, file-provider watch enabled, ping on `health`, JSON logs, dropped
  access-log headers, disabled version checks, and no API/dashboard router.
- [ ] Add a non-active example route for `app.example.com` targeting
  `http://example-service:8080`.
- [ ] Ignore `gateway/dynamic/` while retaining the documentation-only example.
- [ ] Re-run the gateway asset tests and the full unit suite.
- [ ] Commit with `feat: add hardened Traefik gateway assets`.

### Task 2: Add an idempotent remote installer

**Files:**

- Create: `install/install-gateway.sh`
- Modify: `tests/test_gateway_assets.py`

**Interfaces:**

- Consumes: `--host`, `--remote-root`, `--bind-host`, `--bind-port`,
  `--project-name`, `--network-name`, and `--dry-run`.
- Produces: `<remote-root>/services/gateway/{compose.yaml,
  traefik-static.yaml,gateway.env,dynamic/}` and the external Docker network.

- [ ] Add tests for deterministic dry-run output, rejection of non-loopback
  bind hosts, rejection of invalid ports and identifiers, presence of an
  immutable image check, and absence of credential output.
- [ ] Run the focused tests and confirm they fail because the installer is
  absent.
- [ ] Implement strict argument parsing with defaults from
  `REMOTE_RUNNER_SSH_ALIAS` and `REMOTE_RUNNER_ROOT`.
- [ ] Validate the SSH alias and identifiers with allowlist regular expressions,
  require an absolute remote root without `..`, accept only `127.0.0.1`, and
  restrict the port to `1024..65535`.
- [ ] In dry-run mode, print only the target alias, remote root, project,
  network, loopback endpoint, and pinned public image.
- [ ] Stage `compose.yaml` and `traefik-static.yaml` with rsync, then execute a
  positional-argument remote script over SSH.
- [ ] On the server, create the external network when absent, preserve the
  dynamic directory, install public assets, write non-secret `gateway.env` with
  mode `0600`, render `docker compose config`, pull the pinned image, and run
  `docker compose up -d --wait`.
- [ ] Abort before mutation when the selected port is owned by something other
  than the existing gateway project.
- [ ] Re-run focused tests, the full suite, and `bash -n install/*.sh`.
- [ ] Commit with `feat: add remote gateway installer`.

### Task 3: Add remote verification and route reload coverage

**Files:**

- Create: `scripts/verify-gateway.sh`
- Modify: `tests/test_gateway_assets.py`

**Interfaces:**

- Consumes: the same host, root, project, network, bind host, and bind port
  contract as the installer.
- Produces: a non-zero exit code with scoped diagnostics on failure, or a JSON
  summary containing container health, listener, network, and route-reload
  status.

- [ ] Add tests that require verification of container health, read-only root,
  dropped capabilities, absence of Docker-socket mounts, loopback-only listener,
  edge network, initial 404 response, and dynamic route add/remove behavior.
- [ ] Run focused tests and confirm they fail because the verifier is absent.
- [ ] Implement read-only checks for the Compose container, Docker inspect
  hardening fields, external network, and `ss` listener state.
- [ ] Create a uniquely named verification route atomically in the remote
  dynamic directory. Route `gateway-check.invalid/ping` back to Traefik's
  internal `127.0.0.1:8082` ping endpoint.
- [ ] Poll the loopback gateway until the route returns HTTP 200, remove the
  exact verification file on exit, then poll until the same request returns
  HTTP 404 without restarting Traefik.
- [ ] Ensure failure diagnostics use only gateway-scoped Compose logs and never
  print environment contents.
- [ ] Re-run focused tests, the full suite, and shell syntax checks.
- [ ] Commit with `test: add remote gateway verification`.

### Task 4: Document public installation and complete static verification

**Files:**

- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security-model.md`
- Modify: `tests/test_installers.py`

**Interfaces:**

- Consumes: installer and verifier commands from Tasks 2 and 3.
- Produces: generic public documentation using only `remote-docker`,
  `/srv/remote-worktree-runner`, and reserved example hostnames.

- [ ] Add documentation-contract tests requiring gateway install, verify, and
  loopback-only security guidance.
- [ ] Run the documentation tests and confirm they fail before documentation is
  updated.
- [ ] Document dry-run, install, verification, server layout, file-provider
  boundary, and the fact that a healthy gateway has no product routes.
- [ ] Run the full Python suite, shell syntax checks, reproducible archive build,
  `git diff --check`, forbidden-term scan, sensitive-pattern scan, and tracked
  file review.
- [ ] Commit with `docs: document the remote development gateway`.

### Task 5: Deploy and verify on the remote server

**Files:**

- No repository file changes expected.

**Interfaces:**

- Consumes: SSH alias `trie-docker` and remote root `/srv/trie-platform` only as
  runtime arguments; these deployment values are never written to Git.
- Produces: a healthy `remote-worktree-runner-gateway` Compose project and
  `remote-worktree-runner-edge` network on the server.

- [ ] Confirm `127.0.0.1:18080` is free and capture the current gateway/network
  absence without changing other containers.
- [ ] Run the installer with explicit runtime arguments.
- [ ] Run the verifier and require all hardening, listener, health, and dynamic
  reload checks to pass.
- [ ] Independently inspect the container image digest, Compose labels, restart
  policy, mounts, capabilities, read-only root, network, and listener.
- [ ] Confirm `curl http://127.0.0.1:18080/` returns 404 when no route exists.
- [ ] Confirm active runner jobs and their containers were not restarted,
  removed, or reconfigured.
- [ ] Record only non-sensitive verification evidence in the completion report;
  do not commit deployment output.
