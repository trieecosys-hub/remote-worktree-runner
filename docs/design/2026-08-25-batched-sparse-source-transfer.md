# Batched Control Plane and Sparse Source Transfer

## Status

Approved design for implementation after the scheduler resource pool.

## Problem

A one-repository run currently performs separate remote operations for
workspace discovery, reservation, mirror creation, commit push, workspace
preparation, full-tree rsync scanning, worker start, log following, and final
status. SSH multiplexing avoids repeated authentication, but every remote
process still adds latency. Multi-worktree jobs repeat most setup calls per
role.

The remote workspace already starts at the exact selected Git commit. A full
rsync scan is therefore unnecessary when the local worktree is clean and
unnecessarily expensive when only a small overlay changed.

## Goals

- Reduce a normal one-repository run from nine control/transfer sessions to
  four for a clean worktree and five for a dirty worktree.
- Prepare every included worktree through one server request.
- Skip overlay transfer entirely for a clean worktree.
- Transfer only modified and untracked files for a dirty worktree.
- Apply tracked deletions and renames without scanning or deleting unrelated
  paths.
- Combine worker start, live logs, and final status into one reconnect-safe
  execution session.
- Preserve exact commit plus local overlay semantics.
- Remain compatible with an older local client or server during rollout.

## Non-goals

- Mounting the local filesystem on the server.
- Continuous source watching or hot reload.
- Uploading ignored dependency caches, build outputs, credentials, or local
  environment files.
- Replacing Git mirrors or rsync with a proprietary transfer protocol.
- Changing product source code.

## Target flow

For `N` worktree roles, the new flow is:

```text
reserve-and-describe                  1 SSH
git push exact commit                 N SSH-backed Git sessions
prepare-all                           1 SSH
sparse rsync overlay                  0..N SSH-backed rsync sessions
execute (start + logs + final status) 1 SSH
```

A clean primary-only job uses four sessions. A dirty primary-only job uses
five. A four-role clean job uses seven instead of twenty-four.

## Reserve and describe

The client derives deterministic workspace paths from the configured remote
root, repository name, job identifier, and role. It submits the complete job
specification to `reserve` once.

The server validates every derived path, creates all missing bare mirrors, and
returns a description containing protocol version, mirror paths, and workspace
paths. New clients use the returned description. Existing response fields and
the standalone `workspace-path` and `ensure-repo` commands remain available
during compatibility rollout.

## Overlay manifest

Before transfer, the client computes an overlay from Git rather than walking
the full worktree:

- changed tracked paths from `git diff --name-status -z HEAD`;
- untracked, non-ignored paths from
  `git ls-files --others --exclude-standard -z`;
- renamed paths represented as deletion of the old path plus transfer of the
  new path;
- submodule changes rejected with a structured unsupported-overlay error in
  the first implementation.

Every path is repository-relative, NUL-delimited at process boundaries, and
validated to reject absolute paths, `..`, empty components, and paths outside
the worktree. The existing sync exclusion policy is applied after Git
discovery, so credential files and local caches remain excluded even when Git
does not ignore them.

The job specification stores a deterministic manifest per role:

```json
{
  "transfer": ["src/example.py", "tests/test_example.py"],
  "delete": ["src/removed.py"]
}
```

Manifest contents are source paths only. They do not contain file data or
environment values.

## Prepare all

After every exact commit has been pushed, `prepare-all` reads the reserved job
specification and creates or resets every detached worktree. It validates that
all mirrors contain the requested commits, then applies the manifest deletion
set through containment-checked server paths.

The operation is idempotent. Repeating it resets each workspace to the exact
commit before applying deletions again.

## Sparse rsync

For each role with a non-empty transfer set, rsync receives a NUL-delimited
`--files-from` list and the existing exclusion filter. Parent directories are
created as needed, symlinks remain subject to `--safe-links`, and rsync cannot
delete paths because deletion is handled by `prepare-all` from the validated
manifest.

If both transfer and deletion sets are empty, the client skips rsync. The
prepared detached worktree already represents the exact requested source.

## Execute session

The server adds an `execute` command that starts a reserved job and follows its
durable log until a final state. Product log bytes are written to the SSH
session's stderr. The final machine-readable status is written once to stdout.

The local client inherits stderr for immediate logs, captures stdout for the
final JSON record, and returns the exact job exit code. If the SSH session
disconnects, the systemd worker continues and the existing `logs`, `status`,
`cancel`, and `cleanup` commands reconnect normally.

`start` remains available for older clients and for diagnostics.

## Compatibility

The reserve response includes `protocol_version: 2`. A new client receiving a
version 1 response falls back to the current per-role workflow. The new server
continues to support version 1 commands throughout the rollout.

No product repository change is required. Local runner installation updates
the control client, while server installation updates the protocol endpoint.

## Security

- Commands remain argument arrays; the client does not construct a remote
  shell command.
- Mirror, workspace, role, repository, and overlay paths pass existing
  identifier and containment validation.
- Overlay manifests never include ignored secrets or excluded environment
  files.
- Server deletion accepts only paths present in the reserved immutable
  manifest.
- File content continues to use SSH, Git, and rsync encryption and host
  verification.

## Verification

Automated tests cover:

- exact clean-worktree transfer with zero rsync calls;
- modified and untracked file transfer;
- tracked deletion and rename behavior;
- exclusion of environment files, Git internals, dependencies, and build
  output;
- rejection of absolute, parent-traversal, malformed, and submodule paths;
- one batched prepare call for multiple roles;
- execute log streaming, final JSON framing, disconnect, and reconnect;
- protocol version 1 fallback;
- exact session counts for clean and dirty one-role and multi-role jobs.

The remote fixture compares hashes and path sets between a local dirty fixture
worktree and its prepared remote workspace, verifies excluded paths are absent,
then runs a command through `execute` and reconnects after deliberately ending
the first log session.
