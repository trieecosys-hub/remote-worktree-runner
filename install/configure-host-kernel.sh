#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
host=trie-docker
dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) host=${2:?missing host}; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    *) echo "usage: $0 [--host HOST] [--dry-run]" >&2; exit 2 ;;
  esac
done

if [[ ! "$host" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "invalid host: use an SSH alias or hostname" >&2
  exit 2
fi

source_file="$root/config/99-trie-platform-inotify.conf"
destination=/etc/sysctl.d/99-trie-platform-inotify.conf
remote_stage=/tmp/trie-platform-inotify.conf.upload

if $dry_run; then
  echo "would install $destination on $host"
  grep -Ev '^[[:space:]]*(#|$)' "$source_file"
  echo "would apply and verify limits without restarting Docker"
  exit 0
fi

scp "$source_file" "$host:$remote_stage"
ssh -t "$host" \
  "sudo install -o root -g root -m 0644 $remote_stage $destination && sudo /sbin/sysctl --load $destination && rm -f $remote_stage"

actual=$(ssh "$host" "/sbin/sysctl -n fs.inotify.max_user_instances fs.inotify.max_user_watches fs.inotify.max_queued_events")
expected=$'8192\n1048576\n32768'
if [[ "$actual" != "$expected" ]]; then
  echo "host inotify verification failed" >&2
  exit 1
fi
echo "configured and verified host inotify limits on $host"
