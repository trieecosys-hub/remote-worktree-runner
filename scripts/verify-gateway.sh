#!/usr/bin/env bash
set -euo pipefail

host=${REMOTE_RUNNER_SSH_ALIAS:-remote-docker}
remote_root=${REMOTE_RUNNER_ROOT:-/srv/remote-worktree-runner}
bind_host=127.0.0.1
bind_port=18080
project_name=remote-worktree-runner-gateway
network_name=remote-worktree-runner-edge

usage() {
  echo "usage: $0 [--host HOST] [--remote-root PATH] [--bind-host 127.0.0.1] [--bind-port PORT] [--project-name NAME] [--network-name NAME]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) host=${2:?missing host}; shift 2 ;;
    --remote-root) remote_root=${2:?missing remote root}; shift 2 ;;
    --bind-host) bind_host=${2:?missing bind host}; shift 2 ;;
    --bind-port) bind_port=${2:?missing bind port}; shift 2 ;;
    --project-name) project_name=${2:?missing project name}; shift 2 ;;
    --network-name) network_name=${2:?missing network name}; shift 2 ;;
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

ssh "$host" bash -s -- \
  "$remote_root" "$bind_host" "$bind_port" "$project_name" "$network_name" \
  <<'REMOTE_VERIFY'
set -euo pipefail
set -E
remote_root=$1
bind_host=$2
bind_port=$3
project_name=$4
network_name=$5
install_root="$remote_root/services/gateway"
dynamic_root="$install_root/dynamic"
compose=(
  docker compose
  --project-name "$project_name"
  --env-file "$install_root/gateway.env"
  --file "$install_root/compose.yaml"
)
route_file=
temporary_file=

cleanup() {
  [[ -z "$temporary_file" ]] || rm -f -- "$temporary_file"
  [[ -z "$route_file" ]] || rm -f -- "$route_file"
}

diagnose() {
  status=$1
  trap - ERR
  echo "gateway verification failed" >&2
  "${compose[@]}" logs --tail 80 traefik >&2 || true
  exit "$status"
}

trap cleanup EXIT
trap 'diagnose "$?"' ERR

[[ -f "$install_root/compose.yaml" ]]
[[ -f "$install_root/gateway.env" ]]
[[ -d "$dynamic_root" ]]

container_id=$("${compose[@]}" ps --quiet traefik)
[[ -n "$container_id" ]]
[[ $(docker inspect --format '{{.State.Health.Status}}' "$container_id") == healthy ]]
[[ $(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id") == true ]]
[[ $(docker inspect --format '{{json .HostConfig.CapDrop}}' "$container_id") == *'"ALL"'* ]]
[[ $(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$container_id") == unless-stopped ]]

mounts=$(docker inspect --format '{{json .Mounts}}' "$container_id")
[[ "$mounts" != *docker.sock* ]]

docker network inspect "$network_name" >/dev/null
networks=$(docker inspect --format '{{json .NetworkSettings.Networks}}' "$container_id")
[[ "$networks" == *\"$network_name\"* ]]

published_endpoint=$(docker inspect --format \
  '{{(index (index .HostConfig.PortBindings "8080/tcp") 0).HostIp}}:{{(index (index .HostConfig.PortBindings "8080/tcp") 0).HostPort}}' \
  "$container_id")
[[ "$published_endpoint" == "$bind_host:$bind_port" ]]
ss -H -ltn | awk -v expected="$bind_host:$bind_port" -v suffix=":$bind_port" '
  $4 ~ (suffix "$") {matches += 1; if ($4 != expected) bad=1}
  END {exit (matches > 0 && !bad) ? 0 : 1}
'

request_status() {
  curl --silent --show-error --output /dev/null \
    --write-out '%{http_code}' --max-time 3 \
    --header 'Host: gateway-check.invalid' \
    "http://$bind_host:$bind_port/ping" || true
}

status=$(request_status)
[[ "$status" == 404 ]] || {
  echo "expected initial HTTP 404, received $status" >&2
  false
}

temporary_file=$(mktemp "$dynamic_root/.gateway-check.XXXXXX.tmp")
route_file="$dynamic_root/gateway-check-$$.yaml"
cat >"$temporary_file" <<'ROUTE'
http:
  routers:
    gateway-check:
      entryPoints:
        - web
      rule: "Host(`gateway-check.invalid`)"
      service: gateway-check
  services:
    gateway-check:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:8082"
ROUTE
chmod 0644 "$temporary_file"
mv "$temporary_file" "$route_file"
temporary_file=

status=
for _attempt in $(seq 1 40); do
  status=$(request_status)
  [[ "$status" != 200 ]] || break
  sleep 0.25
done
[[ "$status" == 200 ]] || {
  echo "expected dynamic route HTTP 200, received $status" >&2
  false
}

rm -f -- "$route_file"
route_file=
status=
for _attempt in $(seq 1 40); do
  status=$(request_status)
  [[ "$status" != 404 ]] || break
  sleep 0.25
done
[[ "$status" == 404 ]] || {
  echo "expected post-removal HTTP 404, received $status" >&2
  false
}

printf '{"container_health":"healthy","listener":"%s:%s","network":"%s","route_reload":"passed"}\n' \
  "$bind_host" "$bind_port" "$network_name"
REMOTE_VERIFY
