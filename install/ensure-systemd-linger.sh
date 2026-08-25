#!/usr/bin/env bash
set -euo pipefail

runner_user=$(id -un)
runner_uid=$(id -u)
linger=$(loginctl show-user "$runner_uid" --property=Linger --value)
if [[ "$linger" != "yes" ]]; then
  if ! loginctl enable-linger "$runner_user" 2>/dev/null && \
     ! sudo -n loginctl enable-linger "$runner_user"; then
    echo "unable to enable systemd lingering; run 'sudo loginctl enable-linger $runner_user' on the server" >&2
    exit 1
  fi
fi

linger=$(loginctl show-user "$runner_uid" --property=Linger --value)
if [[ "$linger" != "yes" ]]; then
  echo "systemd lingering is not enabled for $runner_user" >&2
  exit 1
fi
