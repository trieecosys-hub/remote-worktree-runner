# Contributing

Contributions are welcome through GitHub issues and pull requests.

## Development setup

The runtime uses only the Python standard library. Python 3.10 or newer is required.

```bash
PYTHONPATH=src python3 -u -m unittest discover -s tests -v
bash -n install/*.sh bin/* scripts/*.sh
git diff --check
```

Run `install/build-zipapp.sh` twice when changing packaged source and confirm that both SHA-256 outputs match.

## Change expectations

- Add a regression test before fixing a defect.
- Keep Docker cleanup scoped to an exact job; never introduce daemon-wide prune or restart behavior.
- Preserve argument arrays across SSH boundaries instead of rebuilding shell commands.
- Keep installer downloads version-pinned and checksum-verified.
- Do not commit credentials, private infrastructure details, personal filesystem paths, or generated build output.
- Update README and security documentation when changing the public interface or trust boundary.

By submitting a contribution, you agree that it is licensed under Apache-2.0.
