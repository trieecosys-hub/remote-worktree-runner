#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
  esac
done

share_dir="$HOME/.local/share/trie-platform"
bin_dir="$HOME/.local/bin"
config_dir="$HOME/.config/trie-platform"

if $dry_run; then
  echo "would install zipapp: $share_dir/trie-remote.pyz"
  echo "would install wrapper: $bin_dir/trie-run"
  echo "would install sync policy: $config_dir/sync-excludes.txt"
  exit 0
fi

"$root/install/build-zipapp.sh"
mkdir -p "$share_dir" "$bin_dir" "$config_dir"
cp "$root/dist/trie-remote.pyz" "$share_dir/trie-remote.pyz"
cp "$root/config/sync-excludes.txt" "$config_dir/sync-excludes.txt"
chmod 0755 "$share_dir/trie-remote.pyz"
chmod 0644 "$config_dir/sync-excludes.txt"

wrapper_tmp=$(mktemp)
trap 'rm -f "$wrapper_tmp"' EXIT
printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
  'exec python3 "$HOME/.local/share/trie-platform/trie-remote.pyz" local "$@"' \
  >"$wrapper_tmp"
install -m 0755 "$wrapper_tmp" "$bin_dir/trie-run"
echo "installed trie-run"
