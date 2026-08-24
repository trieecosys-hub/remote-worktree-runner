#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck disable=SC1091
source "$repository_root/config/versions.env"

host=${REMOTE_RUNNER_SSH_ALIAS:-remote-docker}
remote_root=${REMOTE_RUNNER_ROOT:-/srv/remote-worktree-runner}
bind_host=127.0.0.1
bind_port=18080
project_name=remote-worktree-runner-gateway
network_name=remote-worktree-runner-edge
dry_run=false

usage() {
  echo "usage: $0 [--host HOST] [--remote-root PATH] [--bind-host 127.0.0.1] [--bind-port PORT] [--project-name NAME] [--network-name NAME] [--dry-run]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) host=${2:?missing host}; shift 2 ;;
    --remote-root) remote_root=${2:?missing remote root}; shift 2 ;;
    --bind-host) bind_host=${2:?missing bind host}; shift 2 ;;
    --bind-port) bind_port=${2:?missing bind port}; shift 2 ;;
    --project-name) project_name=${2:?missing project name}; shift 2 ;;
    --network-name) network_name=${2:?missing network name}; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
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
if [[ "$bind_host" != "127.0.0.1" ]]; then
  echo "invalid bind host: the gateway must bind to 127.0.0.1" >&2
  exit 2
fi
if [[ ! "$bind_port" =~ ^[0-9]+$ ]] || \
   (( 10#$bind_port < 1024 || 10#$bind_port > 65535 )); then
  echo "invalid bind port: use an integer from 1024 through 65535" >&2
  exit 2
fi
for identifier in "$project_name" "$network_name"; do
  if [[ ! "$identifier" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    echo "invalid project or network name" >&2
    exit 2
  fi
done
if [[ ! "${TRAEFIK_IMAGE:-}" =~ ^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "TRAEFIK_IMAGE must be pinned by sha256 digest" >&2
  exit 1
fi

if $dry_run; then
  echo "target: $host:$remote_root"
  echo "project: $project_name"
  echo "network: $network_name"
  echo "endpoint: http://$bind_host:$bind_port"
  echo "image: $TRAEFIK_IMAGE"
  exit 0
fi

ssh "$host" bash -s -- "$bind_port" "$project_name" <<'REMOTE_PREFLIGHT'
set -euo pipefail
bind_port=$1
project_name=$2

docker_owners=$(docker ps --filter "publish=$bind_port" \
  --format '{{.ID}} {{.Label "com.docker.compose.project"}}')
if [[ -n "$docker_owners" ]] && \
   awk -v project="$project_name" \
     'BEGIN {bad=0} $2 != project {bad=1} END {exit bad ? 0 : 1}' \
     <<<"$docker_owners"; then
  echo "port $bind_port is published by another Docker project" >&2
  exit 1
fi

if ss -H -ltn | awk -v suffix=":$bind_port" \
  '$4 ~ (suffix "$") {found=1} END {exit !found}'; then
  if [[ -z "$docker_owners" ]]; then
    echo "port $bind_port already has a non-gateway listener" >&2
    exit 1
  fi
fi
REMOTE_PREFLIGHT

remote_stage=$(ssh "$host" bash -s -- "$remote_root" <<'REMOTE_STAGE'
set -euo pipefail
remote_root=$1
install -d -m 0750 "$remote_root/services"
mktemp -d "$remote_root/services/.gateway-upload.XXXXXX"
REMOTE_STAGE
)

cleanup() {
  ssh "$host" bash -s -- "$remote_stage" >/dev/null 2>&1 <<'REMOTE_CLEANUP' || true
set -euo pipefail
remote_stage=$1
rm -rf -- "$remote_stage"
REMOTE_CLEANUP
}
trap cleanup EXIT

rsync -a \
  "$repository_root/gateway/compose.yaml" \
  "$repository_root/gateway/traefik-static.yaml" \
  "$host:$remote_stage/"

ssh "$host" bash -s -- \
  "$remote_root" "$remote_stage" "$bind_host" "$bind_port" \
  "$project_name" "$network_name" "$TRAEFIK_IMAGE" <<'REMOTE_INSTALL'
set -euo pipefail
remote_root=$1
remote_stage=$2
bind_host=$3
bind_port=$4
project_name=$5
network_name=$6
traefik_image=$7
install_root="$remote_root/services/gateway"

docker_owners=$(docker ps --filter "publish=$bind_port" \
  --format '{{.ID}} {{.Label "com.docker.compose.project"}}')
if [[ -n "$docker_owners" ]] && \
   awk -v project="$project_name" \
     'BEGIN {bad=0} $2 != project {bad=1} END {exit bad ? 0 : 1}' \
     <<<"$docker_owners"; then
  echo "port $bind_port is published by another Docker project" >&2
  exit 1
fi
if ss -H -ltn | awk -v suffix=":$bind_port" \
  '$4 ~ (suffix "$") {found=1} END {exit !found}' && \
   [[ -z "$docker_owners" ]]; then
  echo "port $bind_port already has a non-gateway listener" >&2
  exit 1
fi

install -d -m 0750 "$install_root"
install -d -m 0755 "$install_root/dynamic"
install -m 0644 "$remote_stage/compose.yaml" "$install_root/compose.yaml"
install -m 0644 \
  "$remote_stage/traefik-static.yaml" \
  "$install_root/traefik-static.yaml"

umask 077
{
  printf 'TRAEFIK_IMAGE=%s\n' "$traefik_image"
  printf 'GATEWAY_BIND_HOST=%s\n' "$bind_host"
  printf 'GATEWAY_BIND_PORT=%s\n' "$bind_port"
  printf 'GATEWAY_EDGE_NETWORK=%s\n' "$network_name"
} >"$install_root/gateway.env.upload"
mv "$install_root/gateway.env.upload" "$install_root/gateway.env"

docker compose \
  --project-name "$project_name" \
  --env-file "$install_root/gateway.env" \
  --file "$install_root/compose.yaml" \
  config --quiet

if ! docker network inspect "$network_name" >/dev/null 2>&1; then
  docker network create --driver bridge "$network_name" >/dev/null
fi

docker compose \
  --project-name "$project_name" \
  --env-file "$install_root/gateway.env" \
  --file "$install_root/compose.yaml" \
  pull
docker compose \
  --project-name "$project_name" \
  --env-file "$install_root/gateway.env" \
  --file "$install_root/compose.yaml" \
  up --detach --wait --wait-timeout 90
REMOTE_INSTALL

echo "gateway installed on $host at http://$bind_host:$bind_port"
