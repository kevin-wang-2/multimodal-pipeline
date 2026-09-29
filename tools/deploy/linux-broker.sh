#!/usr/bin/env bash
set -Eeuo pipefail

sha=${1:?expected SHA is required}
archive=${2:?archive path is required}
root=${3:?deployment root is required}
config=${4:?broker config path is required}
pm2_name=${5:?PM2 process name is required}
health_url=${6:?health URL is required}

[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid SHA: $sha" >&2; exit 2; }
[[ "$archive" == /tmp/mmp-broker-"$sha".tar.gz ]] || { echo "unexpected archive path" >&2; exit 2; }
[[ -f "$archive" ]] || { echo "archive not found: $archive" >&2; exit 2; }
[[ -f "$config" ]] || { echo "broker config not found: $config" >&2; exit 2; }
command -v pnpm >/dev/null
command -v pm2 >/dev/null
command -v curl >/dev/null

releases="$root/releases"
release="$releases/$sha"
current="$root/src"
staging="$releases/.staging-$sha-$$"
previous=$(readlink -f "$current")
switched=0

cleanup() {
  rm -f "$archive"
  if [[ -d "$staging" ]]; then
    mv "$staging" "$releases/failed-$sha-$$"
  fi
}

rollback() {
  rc=$?
  if (( switched )); then
    echo "deployment failed; rolling back to $previous" >&2
    ln -sfn "$previous" "$current"
    pm2 restart "$pm2_name" >/dev/null
  fi
  exit "$rc"
}

trap cleanup EXIT
trap rollback ERR

mkdir -p "$releases"
if [[ -e "$release" ]]; then
  [[ -f "$release/REVISION" ]] || { echo "existing release lacks REVISION" >&2; exit 2; }
  [[ "$(tr -d '\r\n' < "$release/REVISION")" == "$sha" ]] || { echo "existing release SHA mismatch" >&2; exit 2; }
else
  mkdir "$staging"
  tar -xzf "$archive" -C "$staging"
  [[ "$(tr -d '\r\n' < "$staging/REVISION")" == "$sha" ]] || { echo "archive SHA mismatch" >&2; exit 2; }
  (
    cd "$staging/ts"
    pnpm install --frozen-lockfile
    pnpm -r build
  )
  mv "$staging" "$release"
fi

[[ -L "$current" ]] || { echo "$current must be a symlink before automated deployment" >&2; exit 2; }
ln -sfn "$release" "$current"
switched=1
pm2 restart "$pm2_name" >/dev/null

healthy=0
for _ in $(seq 1 45); do
  if body=$(curl -fsS --max-time 5 "$health_url") && grep -q '"status":"ok"' <<<"$body"; then
    healthy=1
    break
  fi
  sleep 2
done
if (( healthy != 1 )); then
  echo "broker did not become healthy with a registered node" >&2
  false
fi

pm2 save >/dev/null
switched=0
echo "deployed broker $sha (previous $previous)"
