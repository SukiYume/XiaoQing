#!/usr/bin/env bash
# Preview or synchronize the current XiaoQing working tree to production.

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"

# Production target. Edit these values here; they are intentionally not CLI or
# environment parameters. REMOTE_HOST may be changed between secondary-production-host and production-host.
readonly REMOTE_HOST="secondary-production-host"
readonly REMOTE_DIR="/c/Users/testuser/Desktop/XiaoQing/XiaoQing_V3"

readonly SSH_BIN="ssh"
readonly RSYNC_BIN="rsync"
readonly SENTINEL_NAME=".xiaoqing-sync-root"
readonly SENTINEL_VALUE="xiaoqing-sync-root-v1"
readonly ARXIV_MODEL_DIR="plugins/arxiv_filter/best_model"
readonly -a ARXIV_MODEL_FILES=(
    "$ARXIV_MODEL_DIR/config.json"
    "$ARXIV_MODEL_DIR/model.safetensors"
    "$ARXIV_MODEL_DIR/tokenizer.json"
    "$ARXIV_MODEL_DIR/tokenizer_config.json"
    "$ARXIV_MODEL_DIR/training_config.json"
)
readonly -a REMOTE_REQUIRED_FILES=(
    "main.py"
    "pyproject.toml"
    "scripts/run-bot.vbs"
    "scripts/run-bot-monitor.ps1"
    "scripts/run_process_with_rotating_logs.py"
    "${ARXIV_MODEL_FILES[@]}"
)

usage() {
    cat <<'USAGE'
Usage:
  ./scripts/sync_to_remote.sh [--dry-run]
  ./scripts/sync_to_remote.sh --apply --confirm-delete

The default is a dry run. Apply synchronizes the current working tree and
the required arXiv runtime model, and deletes stale remote source files.

Production config, secrets, Minecraft connection config, logs and runtime data
are preserved. The script does not stop or start the production processes.

Edit REMOTE_HOST and REMOTE_DIR near the top of this file to change the target.
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
[[ -f "$REPO_DIR/.gitignore" ]] || die "repository .gitignore is missing"
[[ "$REMOTE_HOST" =~ ^[A-Za-z0-9_.@:-]+$ ]] || die "unsafe remote host"
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ && "$REMOTE_DIR" != "/" ]] \
    || die "remote directory must be a safe non-root absolute path"
[[ "$ARXIV_MODEL_DIR" =~ ^[A-Za-z0-9._/-]+$ \
    && "$ARXIV_MODEL_DIR" != /* \
    && "$ARXIV_MODEL_DIR" != *".."* ]] \
    || die "arXiv model directory must be a safe repository-relative path"
command -v "$SSH_BIN" >/dev/null || die "ssh is required"
command -v "$RSYNC_BIN" >/dev/null || die "rsync is required"

for required_file in "${REMOTE_REQUIRED_FILES[@]}"; do
    [[ -s "$REPO_DIR/$required_file" ]] \
        || die "required release file is missing or empty: $required_file"
done

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
    --checksum
    --human-readable
    --itemize-changes
    --delete-delay
    --delay-updates
    --partial
    --filter='P /.git/***'
    --filter='P /config/config.json'
    --filter='P /config/secrets.json'
    --filter='P /plugins/minecraft/config.json'
    --filter='P /logs/***'
    --filter='P /test_reports/runs/***'
    --filter='P /data/***'
    --filter='P /plugins/*/data/***'
    --filter='P /plugins/*/cache/***'
    --filter='P /plugins/*/backups/***'
    --filter='P /plugins/*/exports/***'
    --include='/.env.example'
    --include='/.env.*.example'
    --include="/$ARXIV_MODEL_DIR/***"
    --exclude='/.git/'
    --exclude='/plugins/*/backups/'
    --exclude='/plugins/*/exports/'
    --exclude-from="$REPO_DIR/.gitignore"
)

source_dir="${REPO_DIR%/}/"
target="${REMOTE_HOST}:${remote_root%/}/"
if [[ "$mode" == "dry-run" ]]; then
    rsync_args+=(--dry-run)
    printf 'Dry run: %s -> %s\n' "$source_dir" "$target"
else
    rsync_args+=(--info=progress2)
    printf 'Applying sync: %s -> %s\n' "$source_dir" "$target"
fi
printf 'Required release asset: %s\n' "$ARXIV_MODEL_DIR"
"$RSYNC_BIN" "${rsync_args[@]}" "$source_dir" "$target"

if [[ "$mode" == "dry-run" ]]; then
    printf 'Dry run complete; no remote files were changed.\n'
    exit 0
fi

"$SSH_BIN" "$REMOTE_HOST" sh -s -- \
    "$remote_root" "${REMOTE_REQUIRED_FILES[@]}" <<'REMOTE'
set -eu
target=$1
shift
cd "$target"
for required_file do
    if ! test -s "$required_file"; then
        printf 'remote release file is missing or empty: %s\n' "$required_file" >&2
        exit 1
    fi
done
REMOTE

printf 'Sync complete; required remote code and arXiv model files are present.\n'
printf 'Production processes were not stopped or started.\n'
