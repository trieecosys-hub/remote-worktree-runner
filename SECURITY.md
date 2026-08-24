# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository. Do not open a public issue for a suspected vulnerability, exposed credential, path escape, command injection, or cross-job isolation failure.

Include the affected revision, deployment topology, reproduction steps, impact, and any suggested mitigation. Maintainers will acknowledge a complete report as soon as practical and coordinate disclosure after a fix is available.

## Supported versions

Security fixes are applied to the latest release on `main`. Older snapshots are not supported unless a release note states otherwise.

## Deployment responsibility

Operators must protect the SSH account, remote Docker socket, server filesystem, and any environment files. The runner reduces accidental cross-job and daemon-wide damage; it is not a sandbox for hostile repository code.
