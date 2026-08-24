## Summary

Describe the problem and the smallest change that solves it.

## Verification

- [ ] Added or updated regression tests where behavior changed
- [ ] `PYTHONPATH=src python3 -u -m unittest discover -s tests -v`
- [ ] `bash -n install/*.sh bin/* scripts/*.sh`
- [ ] Deterministic zipapp comparison when packaged source changed
- [ ] `git diff --check`

## Safety

- [ ] Cleanup remains scoped to an exact job
- [ ] No daemon-wide Docker operation was introduced
- [ ] No credential, personal path, or private infrastructure detail is included
- [ ] Documentation reflects any public interface or trust-boundary change
