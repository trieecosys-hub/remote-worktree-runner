#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
host=${REMOTE_RUNNER_SSH_ALIAS:-${TRIE_REMOTE_HOST:-trie-docker}}
remote_root=${REMOTE_RUNNER_ROOT:-${TRIE_REMOTE_ROOT:-/srv/trie-platform}}
trie_run=${TRIE_RUN_BIN:-$HOME/.local/bin/trie-run}
repository=$(basename "$(git -C "$root" rev-parse --show-toplevel)")
stamp=$(date -u +%Y%m%d%H%M%S)
heavy_one="runner-heavy-one-$stamp"
heavy_two="runner-heavy-two-$stamp"
exclusive="runner-exclusive-$stamp"
fixture_jobs=("$heavy_one" "$heavy_two" "$exclusive")
started_jobs=()
stage=$(mktemp -d)

cleanup() {
  local fixture_job
  for fixture_job in "${started_jobs[@]}"; do
    "$trie_run" cancel "$fixture_job" >/dev/null 2>&1 || true
  done
  for fixture_job in "${started_jobs[@]}"; do
    local deadline=$((SECONDS + 30))
    while (( SECONDS < deadline )); do
      local state
      state=$("$trie_run" status "$fixture_job" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state", ""))' 2>/dev/null || true)
      case "$state" in
        passed|failed|cancelled|not-found) break ;;
      esac
      sleep 1
    done
    "$trie_run" cleanup "$fixture_job" --volumes >/dev/null 2>&1 || true
  done
  rm -rf "$stage"
}
trap cleanup EXIT

test -x "$trie_run"
test -f "$HOME/.local/share/trie-platform/trie-remote.pyz"

local_hash=$(shasum -a 256 "$HOME/.local/share/trie-platform/trie-remote.pyz" | awk '{print $1}')
server_hash=$(ssh "$host" sha256sum "$remote_root/bin/trie-remote.pyz" | awk '{print $1}')
test "$local_hash" = "$server_hash"

doctor_file="$stage/doctor.json"
"$trie_run" doctor --show-sync | tee "$doctor_file"
scheduler_capacity=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["server"]["scheduler_capacity"])' "$doctor_file")
ssh "$host" 'test "$(uname -m)" = x86_64'
ssh "$host" '/usr/bin/docker version --format "server={{.Server.Version}}/{{.Server.Arch}}"'

cd "$root"
started_jobs+=("$heavy_one")
"$trie_run" run --job "$heavy_one" --weight heavy -- bash -lc 'sleep 8' \
  >"$stage/$heavy_one.run" 2>&1 &
heavy_one_pid=$!

started_jobs+=("$heavy_two")
"$trie_run" run --job "$heavy_two" --weight heavy -- bash -lc 'sleep 8' \
  >"$stage/$heavy_two.run" 2>&1 &
heavy_two_pid=$!

wait_until_admitted() {
  local fixture_job=$1
  local deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    local admission
    admission=$("$trie_run" status "$fixture_job" 2>/dev/null | python3 -c 'import json,sys; value=json.load(sys.stdin); state=value.get("state", ""); print("ready" if state in {"running", "passed", "failed", "cancelled"} or (state == "queued" and "queue_position" in value) else "waiting")' 2>/dev/null || true)
    case "$admission" in
      ready) return 0 ;;
    esac
    sleep 1
  done
  echo "fixture job was not admitted: $fixture_job" >&2
  return 1
}

wait_until_admitted "$heavy_one"
wait_until_admitted "$heavy_two"

started_jobs+=("$exclusive")
"$trie_run" run --job "$exclusive" --weight exclusive -- bash -lc 'sleep 1' \
  >"$stage/$exclusive.run" 2>&1 &
exclusive_pid=$!

wait "$heavy_one_pid"
wait "$heavy_two_pid"
wait "$exclusive_pid"

for fixture_job in "${fixture_jobs[@]}"; do
  "$trie_run" status "$fixture_job" >"$stage/$fixture_job.status"
done

python3 - "$scheduler_capacity" \
  "$stage/$heavy_one.status" \
  "$stage/$heavy_two.status" \
  "$stage/$exclusive.status" <<'PY'
import datetime
import json
import sys

capacity = int(sys.argv[1])
heavy = [json.load(open(path, encoding="utf-8")) for path in sys.argv[2:4]]
exclusive = json.load(open(sys.argv[4], encoding="utf-8"))

def finished_at(status):
    value = status["finished_at"]
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.datetime.fromisoformat(value).timestamp()

if any(status["state"] != "passed" for status in [*heavy, exclusive]):
    raise SystemExit("scheduler fixture did not pass")
latest_heavy_start = max(float(status["started_at"]) for status in heavy)
earliest_heavy_finish = min(finished_at(status) for status in heavy)
if capacity >= 2 and latest_heavy_start >= earliest_heavy_finish:
    raise SystemExit("heavy jobs did not overlap")
if float(exclusive["started_at"]) < max(finished_at(status) for status in heavy):
    raise SystemExit("exclusive job bypassed heavy jobs")
PY

for fixture_job in "${fixture_jobs[@]}"; do
  "$trie_run" cleanup "$fixture_job" --volumes
done
started_jobs=()

for fixture_job in "${fixture_jobs[@]}"; do
  resource="trie-$repository-$fixture_job"
  if ssh "$host" "/usr/bin/docker ps -a --format '{{.Names}}'; /usr/bin/docker volume ls --format '{{.Name}}'" | grep -F "$resource"; then
    echo "scoped Docker resources remain after cleanup: $resource" >&2
    exit 1
  fi
done

echo "deployment verification passed: capacity=$scheduler_capacity artifact=$local_hash"
