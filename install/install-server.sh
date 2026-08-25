#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck disable=SC1091
source "$root/config/versions.env"
host=trie-docker
remote_root=/srv/trie-platform
repositories=trie-vms,trie-center,trie-process,trie-space,trie-platform-ops
max_heavy_jobs=1
dry_run=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) host=${2:?missing host}; shift 2 ;;
    --remote-root) remote_root=${2:?missing remote root}; shift 2 ;;
    --repositories) repositories=${2:?missing repositories}; shift 2 ;;
    --max-heavy-jobs) max_heavy_jobs=${2:?missing max heavy jobs}; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    *) echo "usage: $0 [--host HOST] [--remote-root PATH] [--repositories CSV] [--max-heavy-jobs COUNT] [--dry-run]" >&2; exit 2 ;;
  esac
done

if [[ ! "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ ]] || \
   [[ "$remote_root" == "/" || "$remote_root" == *"//"* || "/$remote_root/" == *"/../"* || "/$remote_root/" == *"/./"* ]]; then
  echo "invalid remote root: use a normalized absolute path" >&2
  exit 2
fi
if [[ ! "$repositories" =~ ^[a-z0-9][a-z0-9-]{0,62}(,[a-z0-9][a-z0-9-]{0,62})*$ ]]; then
  echo "invalid repositories: use comma-separated lowercase identifiers" >&2
  exit 2
fi
if [[ ! "$max_heavy_jobs" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid max heavy jobs: use a positive integer" >&2
  exit 2
fi
if $dry_run; then
  echo "would verify and install runner on $host:$remote_root/bin"
  echo "would allow repositories: $repositories"
  echo "would configure $max_heavy_jobs concurrent heavy jobs"
  echo "would enable and verify systemd lingering for the remote runner account"
  echo "would download checksum-verified kubectl $KUBECTL_VERSION for linux/amd64"
  echo "would download checksum-verified Kind $KIND_VERSION for linux/amd64"
  echo "would download checksum-verified jq $JQ_VERSION for linux/amd64"
  echo "would download checksum-verified ripgrep $RIPGREP_VERSION for linux/amd64"
  echo "would download checksum-verified Node.js $NODE_PROCESS_VERSION for TrieProcess"
  echo "would download checksum-verified Node.js $NODE_CENTER_VERSION for TrieCenter"
  echo "would download checksum-verified Go $GO_VERSION for linux/amd64"
  exit 0
fi

ssh "$host" 'bash -s' <"$root/install/ensure-systemd-linger.sh"

"$root/install/build-zipapp.sh"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

kubectl_url="https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl"
curl -fsSLo "$stage/kubectl" "$kubectl_url"
curl -fsSLo "$stage/kubectl.sha256" "$kubectl_url.sha256"
kubectl_expected=$(tr -d '[:space:]' <"$stage/kubectl.sha256")
kubectl_actual=$(shasum -a 256 "$stage/kubectl" | awk '{print $1}')
[[ "$kubectl_actual" == "$kubectl_expected" ]] || { echo "kubectl sha256 mismatch" >&2; exit 1; }

kind_url="https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64"
curl -fsSLo "$stage/kind" "$kind_url"
curl -fsSLo "$stage/kind.sha256" "$kind_url.sha256sum"
kind_expected=$(awk '{print $1}' "$stage/kind.sha256")
kind_actual=$(shasum -a 256 "$stage/kind" | awk '{print $1}')
[[ "$kind_actual" == "$kind_expected" ]] || { echo "Kind sha256 mismatch" >&2; exit 1; }

jq_url="https://github.com/jqlang/jq/releases/download/jq-$JQ_VERSION/jq-linux-amd64"
curl -fsSLo "$stage/jq" "$jq_url"
jq_actual=$(shasum -a 256 "$stage/jq" | awk '{print $1}')
[[ "$jq_actual" == "$JQ_LINUX_AMD64_SHA256" ]] || { echo "jq sha256 mismatch" >&2; exit 1; }

ripgrep_filename="ripgrep_${RIPGREP_VERSION}-1_amd64.deb"
ripgrep_url="https://github.com/BurntSushi/ripgrep/releases/download/$RIPGREP_VERSION/$ripgrep_filename"
curl -fsSLo "$stage/$ripgrep_filename" "$ripgrep_url"
ripgrep_actual=$(shasum -a 256 "$stage/$ripgrep_filename" | awk '{print $1}')
[[ "$ripgrep_actual" == "$RIPGREP_LINUX_AMD64_SHA256" ]] || { echo "ripgrep sha256 mismatch" >&2; exit 1; }
mkdir -p "$stage/ripgrep-deb" "$stage/ripgrep-root"
tar -xf "$stage/$ripgrep_filename" -C "$stage/ripgrep-deb" data.tar.xz
tar -xJf "$stage/ripgrep-deb/data.tar.xz" -C "$stage/ripgrep-root" ./usr/bin/rg

download_node() {
  local version=$1
  local destination=$2
  local filename="node-$version-linux-x64.tar.gz"
  local release_url="https://nodejs.org/download/release/$version"
  curl -fsSLo "$stage/$filename" "$release_url/$filename"
  curl -fsSLo "$stage/$filename.SHASUMS256.txt" "$release_url/SHASUMS256.txt"
  local expected
  expected=$(awk -v filename="$filename" '$2 == filename {print $1}' "$stage/$filename.SHASUMS256.txt")
  [[ -n "$expected" ]] || { echo "Node.js $version checksum is unavailable" >&2; exit 1; }
  local actual
  actual=$(shasum -a 256 "$stage/$filename" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || { echo "Node.js $version sha256 mismatch" >&2; exit 1; }
  mkdir -p "$stage/$destination"
  tar -xzf "$stage/$filename" --strip-components=1 -C "$stage/$destination"
}

download_node "$NODE_PROCESS_VERSION" node-process
download_node "$NODE_CENTER_VERSION" node-center

go_filename="go$GO_VERSION.linux-amd64.tar.gz"
curl -fsSLo "$stage/$go_filename" "https://go.dev/dl/$go_filename"
go_actual=$(shasum -a 256 "$stage/$go_filename" | awk '{print $1}')
[[ "$go_actual" == "$GO_LINUX_AMD64_SHA256" ]] || { echo "Go sha256 mismatch" >&2; exit 1; }
mkdir -p "$stage/go-toolchain"
tar -xzf "$stage/$go_filename" --strip-components=1 -C "$stage/go-toolchain"

ssh "$host" "mkdir -p $remote_root/bin $remote_root/repos $remote_root/workspaces $remote_root/jobs $remote_root/caches $remote_root/locks $remote_root/environments $remote_root/toolchains && chmod 0750 $remote_root $remote_root/bin $remote_root/repos $remote_root/workspaces $remote_root/jobs $remote_root/caches $remote_root/locks $remote_root/environments $remote_root/toolchains"
scp "$root/dist/trie-remote.pyz" "$host:$remote_root/bin/trie-remote.pyz.upload"
scp "$stage/kubectl" "$host:$remote_root/bin/kubectl.upload"
scp "$stage/kind" "$host:$remote_root/bin/kind.upload"
scp "$stage/jq" "$host:$remote_root/bin/jq.upload"
scp "$stage/ripgrep-root/usr/bin/rg" "$host:$remote_root/bin/rg.upload"
ssh "$host" "chmod 0755 $remote_root/bin/trie-remote.pyz.upload $remote_root/bin/kubectl.upload $remote_root/bin/kind.upload $remote_root/bin/jq.upload $remote_root/bin/rg.upload && mv $remote_root/bin/trie-remote.pyz.upload $remote_root/bin/trie-remote.pyz && mv $remote_root/bin/kubectl.upload $remote_root/bin/kubectl && mv $remote_root/bin/kind.upload $remote_root/bin/kind && mv $remote_root/bin/jq.upload $remote_root/bin/jq && mv $remote_root/bin/rg.upload $remote_root/bin/rg"

rsync -a --delete "$stage/node-process/" "$host:$remote_root/toolchains/node-$NODE_PROCESS_VERSION/"
rsync -a --delete "$stage/node-center/" "$host:$remote_root/toolchains/node-$NODE_CENTER_VERSION/"
rsync -a --delete "$stage/go-toolchain/" "$host:$remote_root/toolchains/go-$GO_VERSION/"
ssh "$host" "ln -sfn $remote_root/toolchains/node-$NODE_PROCESS_VERSION $remote_root/toolchains/process-node && ln -sfn $remote_root/toolchains/node-$NODE_CENTER_VERSION $remote_root/toolchains/center-node && ln -sfn $remote_root/toolchains/go-$GO_VERSION $remote_root/toolchains/go"

wrapper_tmp="$stage/trie-runner"
printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
  "export REMOTE_RUNNER_ROOT=\"$remote_root\"" \
  "export REMOTE_RUNNER_ALLOWED_REPOSITORIES=\"$repositories\"" \
  "export REMOTE_RUNNER_MAX_HEAVY_JOBS=\"$max_heavy_jobs\"" \
  "exec /usr/bin/python3 \"$remote_root/bin/trie-remote.pyz\" server \"\$@\"" \
  >"$wrapper_tmp"
chmod 0755 "$wrapper_tmp"
scp "$wrapper_tmp" "$host:$remote_root/bin/trie-runner.upload"
ssh "$host" "chmod 0755 $remote_root/bin/trie-runner.upload && mv $remote_root/bin/trie-runner.upload $remote_root/bin/trie-runner"
echo "installed trie-runner, kubectl $KUBECTL_VERSION, Kind $KIND_VERSION, jq $JQ_VERSION, ripgrep $RIPGREP_VERSION, Node.js $NODE_PROCESS_VERSION/$NODE_CENTER_VERSION, and Go $GO_VERSION on $host"
