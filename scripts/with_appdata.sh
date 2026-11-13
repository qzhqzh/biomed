#!/usr/bin/env bash
# Host-side development/research caches; does not change shared model storage.
set -euo pipefail
if [[ $# -eq 0 ]]; then
  echo 'Usage: scripts/with_appdata.sh COMMAND [ARG ...]' >&2
  exit 2
fi
if [[ "$(findmnt -n -o TARGET -T /appdata)" != /appdata ]]; then
  echo '/appdata is not mounted; refusing to create caches on the root filesystem.' >&2
  exit 1
fi
biomed_work_dir=${BIOMED_WORK_DIR:-/appdata/02_resource/apps/biomed}
case "$biomed_work_dir" in /appdata/*) ;; *) echo 'BIOMED_WORK_DIR must be below /appdata.' >&2; exit 2 ;; esac
export UV_CACHE_DIR="$biomed_work_dir/development/cache/uv"
export PIP_CACHE_DIR="$biomed_work_dir/development/cache/pip"
export npm_config_cache="$biomed_work_dir/development/cache/npm"
export RUFF_CACHE_DIR="$biomed_work_dir/development/cache/ruff"
export PYTHONPYCACHEPREFIX="$biomed_work_dir/development/cache/pycache"
mkdir -p "$UV_CACHE_DIR" "$PIP_CACHE_DIR" "$npm_config_cache" "$RUFF_CACHE_DIR" "$PYTHONPYCACHEPREFIX"
exec "$@"
