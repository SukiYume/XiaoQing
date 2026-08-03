#!/usr/bin/env bash
# Safely preview or sync the current XiaoQing working tree with rsync.

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
readonly REMOTE_HOST="${XIAOQING_SYNC_HOST:-production-host}"
readonly REMOTE_DIR="${XIAOQING_SYNC_DIR:-/c/Users/testuser/Desktop/XiaoQing/XiaoQing_V3}"
readonly SSH_BIN="${XIAOQING_SSH_BIN:-ssh}"
readonly RSYNC_BIN="${XIAOQING_RSYNC_BIN:-rsync}"
readonly SENTINEL_NAME=".xiaoqing-sync-root"
readonly SENTINEL_VALUE="xiaoqing-sync-root-v1"

usage() {
    cat <<'USAGE'
Usage:
  ./scripts/sync_to_remote.sh [--dry-run]
  ./scripts/sync_to_remote.sh --apply --confirm-delete

The default is a dry run. Apply synchronizes the current working tree and
deletes stale remote source files, while preserving config, logs and runtime data.
USAGE
}

die() {
    printf 'sync_to_remote.sh: %s\n' "$*" >&2
    exit 1
}

mode="dry-run"
confirm_delete="false"
while (($#)); do
    case "$1" in
        --dry-run) mode="dry-run" ;;
        --apply) mode="apply" ;;
        --confirm-delete) confirm_delete="true" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
    shift
done

if [[ "$mode" == "apply" && "$confirm_delete" != "true" ]]; then
    die "--apply requires --confirm-delete"
fi
if [[ "$mode" != "apply" && "$confirm_delete" == "true" ]]; then
    die "--confirm-delete is valid only with --apply"
fi
[[ -f "$REPO_DIR/$SENTINEL_NAME" ]] || die "repository sentinel is missing"
[[ "$(<"$REPO_DIR/$SENTINEL_NAME")" == "$SENTINEL_VALUE" ]] \
    || die "repository sentinel has an unexpected value"
[[ "$REMOTE_HOST" =~ ^[A-Za-z0-9_.@:-]+$ ]] || die "unsafe remote host"
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ && "$REMOTE_DIR" != "/" ]] \
    || die "remote directory must be a safe non-root absolute path"
command -v "$SSH_BIN" >/dev/null || die "ssh is required"
command -v "$RSYNC_BIN" >/dev/null || die "rsync is required"

remote_root="$($SSH_BIN "$REMOTE_HOST" sh -s -- "$REMOTE_DIR" "$SENTINEL_NAME" "$SENTINEL_VALUE" <<'REMOTE'
set -eu
target=$(readlink -f -- "$1")
test -n "$target" && test "$target" != / && test -d "$target"
test -f "$target/$2" && test "$(cat -- "$target/$2")" = "$3"
printf '%s\n' "$target"
REMOTE
)"
[[ "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ && "$remote_root" != "/" ]] \
    || die "remote target validation failed"

rsync_args=(
    -a
    --human-readable
    --itemize-changes
    --delete-delay
    --filter='P /.git/***'
    --filter='P /config/config.json'
    --filter='P /config/secrets.json'
    --filter='P /logs/***'
    --filter='P /test_reports/runs/***'
    --filter='P /data/***'
    --filter='P /plugins/*/data/***'
    --filter='P /plugins/*/cache/***'
    --exclude='/.git/'
    --exclude='/.pytest_cache/'
    --exclude='/.ruff_cache/'
    --exclude='/.mypy_cache/'
    --exclude='/**/__pycache__/'
    --exclude='/*.log'
    --exclude='/config/config.json'
    --exclude='/config/secrets.json'
    --exclude='/logs/'
    --exclude='/test_reports/runs/'
    --exclude='/data/'
    --exclude='/plugins/*/data/'
    --exclude='/plugins/*/cache/'
    --exclude='/plugins/*/backups/'
    --exclude='/plugins/*/exports/'
    --exclude='/plugins/arxiv_filter/best_model*'
    --exclude='/plugins/arxiv_filter/train_model/**/cache/'
)

source_dir="${REPO_DIR%/}/"
target="${REMOTE_HOST}:${remote_root%/}/"
if [[ "$mode" == "dry-run" ]]; then
    rsync_args+=(--dry-run)
    printf 'Dry run: %s -> %s\n' "$source_dir" "$target"
else
    printf 'Applying sync: %s -> %s\n' "$source_dir" "$target"
fi
"$RSYNC_BIN" "${rsync_args[@]}" "$source_dir" "$target"
