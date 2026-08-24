#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
host=""
remote_root=""
slot=""
trie_run=${TRIE_RUN_BIN:-$HOME/.local/bin/trie-run}

usage() {
  echo "usage: $0 --host HOST --remote-root PATH --slot SLOT" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) host=${2:?missing host}; shift 2 ;;
    --remote-root) remote_root=${2:?missing remote root}; shift 2 ;;
    --slot) slot=${2:?missing slot}; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ ! "$host" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "invalid SSH host alias" >&2
  exit 2
fi
if [[ ! "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ ]] || \
   [[ "$remote_root" == "/" || "$remote_root" == *"//"* || \
      "/$remote_root/" == *"/../"* || "/$remote_root/" == *"/./"* ]]; then
  echo "invalid remote root: use a normalized absolute path" >&2
  exit 2
fi
if [[ ! "$slot" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "invalid preview slot" >&2
  exit 2
fi
if [[ ! -x "$trie_run" ]]; then
  echo "local trie-run is not executable" >&2
  exit 1
fi

stamp="$(date -u +%Y%m%d%H%M%S)-$$"
job_a="preview-check-a-$stamp"
job_b="preview-check-b-$stamp"
project_a="preview-fixture-a-$stamp"
project_b="preview-fixture-b-$stamp"
job_a_started=false
job_b_started=false
active_owner=""
refusal_output=$(mktemp)

export REMOTE_RUNNER_SSH_ALIAS="$host"
export REMOTE_RUNNER_ROOT="$remote_root"

cleanup() {
  status=$?
  trap - EXIT
  if [[ -n "$active_owner" ]]; then
    "$trie_run" preview unpublish \
      --job "$active_owner" --slot "$slot" >/dev/null 2>&1 || true
  fi
  if $job_b_started; then
    "$trie_run" cancel "$job_b" >/dev/null 2>&1 || true
    "$trie_run" cleanup "$job_b" --volumes >/dev/null 2>&1 || true
  fi
  if $job_a_started; then
    "$trie_run" cancel "$job_a" >/dev/null 2>&1 || true
    "$trie_run" cleanup "$job_a" --volumes >/dev/null 2>&1 || true
  fi
  rm -f -- "$refusal_output"
  exit "$status"
}
trap cleanup EXIT

json_field() {
  python3 -c \
    'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

assert_listing() {
  expected_job=$1
  expected_container=$2
  python3 -c '
import json
import sys

slot, job, container = sys.argv[1:]
routes = [route for route in json.load(sys.stdin) if route["slot"] == slot]
if len(routes) != 1:
    raise SystemExit("expected exactly one route for verification slot")
route = routes[0]
if route["job_id"] != job or route["container_id"] != container:
    raise SystemExit("preview listing does not match expected owner")
' "$slot" "$expected_job" "$expected_container"
}

run_fixture() {
  job=$1
  project=$2
  "$trie_run" run --job "$job" --weight light -- \
    bash -lc '
set -euo pipefail
source config/versions.env
export TRAEFIK_IMAGE
docker compose \
  --project-name "$1" \
  --file tests/fixtures/preview-service/compose.yaml \
  up --detach --wait --wait-timeout 60
' fixture "$project"
}

cd "$root"
job_a_started=true
run_fixture "$job_a" "$project_a"
route_a=$("$trie_run" preview publish \
  --job "$job_a" \
  --slot "$slot" \
  --project "$project_a" \
  --service preview \
  --port 8080 \
  --check-path /ping)
active_owner=$job_a
container_a=$(printf '%s' "$route_a" | json_field container_id)
listing=$("$trie_run" preview list)
printf '%s' "$listing" | assert_listing "$job_a" "$container_a"

job_b_started=true
run_fixture "$job_b" "$project_b"
route_b=$("$trie_run" preview publish \
  --job "$job_b" \
  --slot "$slot" \
  --project "$project_b" \
  --service preview \
  --port 8080 \
  --check-path /ping)
active_owner=$job_b
container_b=$(printf '%s' "$route_b" | json_field container_id)
[[ "$container_a" != "$container_b" ]]
listing=$("$trie_run" preview list)
printf '%s' "$listing" | assert_listing "$job_b" "$container_b"

if "$trie_run" cleanup "$job_b" --volumes >"$refusal_output" 2>&1; then
  echo "expected cleanup refusal for an active preview" >&2
  exit 1
fi
if ! grep -F "active previews" "$refusal_output" >/dev/null; then
  echo "cleanup failed without the expected active-preview guard" >&2
  exit 1
fi

"$trie_run" preview unpublish --job "$job_b" --slot "$slot" >/dev/null
active_owner=""
"$trie_run" cleanup "$job_b" --volumes >/dev/null
job_b_started=false
"$trie_run" cleanup "$job_a" --volumes >/dev/null
job_a_started=false

remaining=$(ssh "$host" bash -s -- "$project_a" "$project_b" <<'REMOTE_CHECK'
set -euo pipefail
project_a=$1
project_b=$2
{
  /usr/bin/docker ps --all --quiet \
    --filter "label=com.docker.compose.project=$project_a"
  /usr/bin/docker ps --all --quiet \
    --filter "label=com.docker.compose.project=$project_b"
} | sed '/^$/d'
REMOTE_CHECK
)
if [[ -n "$remaining" ]]; then
  echo "fixture containers remain after exact cleanup" >&2
  exit 1
fi

rm -f -- "$refusal_output"
printf '{"handoff":"passed","slot":"%s"}\n' "$slot"
