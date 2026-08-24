#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
dry_run=false
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=true
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--dry-run]" >&2
  exit 2
fi

output="$root/dist/trie-remote.pyz"
if $dry_run; then
  echo "would build deterministic zipapp: $output"
  exit 0
fi

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$root/dist"
cp -R "$root/src/trie_remote" "$stage/trie_remote"
find "$stage" -type d -name __pycache__ -prune -exec rm -r {} +
printf '%s\n' \
  'from trie_remote.__main__ import main' \
  '' \
  'raise SystemExit(main())' \
  >"$stage/__main__.py"
find "$stage" -exec touch -t 198001010000 {} +
python3 -m zipapp "$stage" \
  -p "/usr/bin/env python3" \
  -o "$output"
chmod 0755 "$output"
shasum -a 256 "$output"
