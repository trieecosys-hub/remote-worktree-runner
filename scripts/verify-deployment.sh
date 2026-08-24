#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
host=${REMOTE_RUNNER_SSH_ALIAS:-${TRIE_REMOTE_HOST:-trie-docker}}
remote_root=${REMOTE_RUNNER_ROOT:-${TRIE_REMOTE_ROOT:-/srv/trie-platform}}
trie_run=${TRIE_RUN_BIN:-$HOME/.local/bin/trie-run}
repository=$(basename "$(git -C "$root" rev-parse --show-toplevel)")
job="runner-final-$(date -u +%Y%m%d%H%M%S)"
resource="trie-$repository-$job"
job_started=false

cleanup() {
  if $job_started; then
    "$trie_run" cleanup "$job" --volumes >/dev/null || true
  fi
}
trap cleanup EXIT

test -x "$trie_run"
test -f "$HOME/.local/share/trie-platform/trie-remote.pyz"

local_hash=$(shasum -a 256 "$HOME/.local/share/trie-platform/trie-remote.pyz" | awk '{print $1}')
server_hash=$(ssh "$host" sha256sum "$remote_root/bin/trie-remote.pyz" | awk '{print $1}')
test "$local_hash" = "$server_hash"

"$trie_run" doctor --show-sync
ssh "$host" 'test "$(uname -m)" = x86_64'
ssh "$host" '/usr/bin/docker version --format "server={{.Server.Version}}/{{.Server.Arch}}"'

cd "$root"
job_started=true
"$trie_run" run --job "$job" --weight light -- \
  bash -lc 'test "$(docker version --format "{{.Server.Arch}}")" = amd64 && docker version --format "server={{.Server.Version}}/{{.Server.Arch}}"'
"$trie_run" cleanup "$job" --volumes
job_started=false

if ssh "$host" "/usr/bin/docker ps -a --format '{{.Names}}'; /usr/bin/docker volume ls --format '{{.Name}}'" | grep -F "$resource"; then
  echo "scoped Docker resources remain after cleanup: $resource" >&2
  exit 1
fi

echo "deployment verification passed: job=$job artifact=$local_hash"
