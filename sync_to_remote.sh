#!/usr/bin/env bash
# Preview or apply an immutable, commit-derived deployment stage.

set -euo pipefail
IFS=$'\n\t'
export LC_ALL=C

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_DIR="$SCRIPT_DIR"
readonly STAGE_HELPER="$REPO_DIR/scripts/build_deploy_stage.py"
readonly DEPLOY_MANIFEST="deploy/runtime-paths.txt"
readonly REMOTE_HOST="${XIAOQING_SYNC_HOST:-production-host}"
readonly REMOTE_DIR="${XIAOQING_SYNC_DIR:-/c/Users/testuser/Desktop/XiaoQing/XiaoQing_V3}"
readonly PYTHON_BIN="${PYTHON:-python}"
readonly SENTINEL_NAME=".xiaoqing-sync-root"
readonly SENTINEL_VALUE="xiaoqing-sync-root-v1"
readonly PLAN_SCHEMA="xiaoqing-rsync-plan-v1"

usage() {
    cat <<'USAGE'
Usage:
  ./sync_to_remote.sh [--dry-run] [--ref <git-ref>]
  ./sync_to_remote.sh --apply --confirm-delete \
      --ref <40-hex-commit> --expect-plan <64-hex-sha256>

Preview resolves the selected ref once, builds a secret-scanned Git archive
stage, prints the rsync dry-run and a Plan SHA256, and changes nothing remotely.
Apply accepts only the exact commit and plan digest printed by a prior preview.

Optional environment overrides:
  XIAOQING_SYNC_HOST=<ssh-host>
  XIAOQING_SYNC_DIR=</absolute/remote/path>
  PYTHON=<python-command>
USAGE
}

die() {
    printf 'sync_to_remote.sh: %s\n' "$*" >&2
    exit 1
}

mode="preview"
ref="HEAD"
ref_explicit="false"
expected_plan=""
confirm_delete="false"
dry_run_flag="false"

while (($#)); do
    case "$1" in
        --dry-run)
            [[ "$dry_run_flag" == "false" ]] || die "--dry-run was provided more than once"
            dry_run_flag="true"
            shift
            ;;
        --apply)
            [[ "$mode" == "preview" ]] || die "--apply was provided more than once"
            mode="apply"
            shift
            ;;
        --confirm-delete)
            [[ "$confirm_delete" == "false" ]] || die "--confirm-delete was provided more than once"
            confirm_delete="true"
            shift
            ;;
        --ref)
            (($# >= 2)) || die "--ref requires a value"
            [[ "$ref_explicit" == "false" ]] || die "--ref was provided more than once"
            ref="$2"
            ref_explicit="true"
            shift 2
            ;;
        --expect-plan)
            (($# >= 2)) || die "--expect-plan requires a value"
            [[ -z "$expected_plan" ]] || die "--expect-plan was provided more than once"
            expected_plan="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown argument: $1"
            ;;
    esac
done

if [[ "$mode" == "apply" ]]; then
    [[ "$dry_run_flag" == "false" ]] || die "--dry-run cannot be combined with --apply"
    [[ "$confirm_delete" == "true" ]] || die "apply requires --confirm-delete"
    [[ "$ref_explicit" == "true" && "$ref" =~ ^[0-9a-fA-F]{40}$ ]] \
        || die "apply requires an explicit 40-hex --ref commit"
    [[ "$expected_plan" =~ ^[0-9a-fA-F]{64}$ ]] \
        || die "apply requires a 64-hex --expect-plan digest"
else
    [[ "$confirm_delete" == "false" ]] || die "--confirm-delete is valid only with --apply"
    [[ -z "$expected_plan" ]] || die "--expect-plan is valid only with --apply"
fi
[[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/@:-]*$ && "$ref" != *..* && "$ref" != *@\{* ]] \
    || die "--ref must be a safe revision name and cannot start with '-'"

[[ -f "$STAGE_HELPER" ]] || die "missing staging helper: $STAGE_HELPER"
[[ "$REMOTE_HOST" =~ ^[A-Za-z0-9_.@:-]+$ ]] || die "unsafe remote host value"
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || die "remote directory must be a safe absolute POSIX path"
[[ "$REMOTE_DIR" != "/" ]] || die "remote directory cannot be filesystem root"

command -v "$PYTHON_BIN" >/dev/null || die "Python is required"
command -v ssh >/dev/null || die "ssh is required"
command -v rsync >/dev/null || die "rsync is required"
command -v mktemp >/dev/null || die "mktemp is required"

temp_root="$(mktemp -d "${TMPDIR:-/tmp}/xiaoqing-sync.XXXXXXXX")"
readonly temp_root
[[ -n "$temp_root" && "$temp_root" != "/" && -d "$temp_root" ]] \
    || die "mktemp returned an unsafe temporary directory"
[[ "$(basename -- "$temp_root")" == xiaoqing-sync.* ]] \
    || die "temporary directory does not have the required xiaoqing-sync prefix"
cleanup_temp_root() {
    if [[ -n "$temp_root" && "$temp_root" != "/" && -d "$temp_root" \
        && "$(basename -- "$temp_root")" == xiaoqing-sync.* ]]; then
        rm -rf -- "$temp_root"
    fi
}
trap cleanup_temp_root EXIT
readonly stage_dir="$temp_root/stage"
readonly metadata_file="$temp_root/stage-metadata.json"
readonly remote_output="$temp_root/remote-root.txt"
readonly dry_run_plan="$temp_root/rsync-dry-run.txt"
readonly options_file="$temp_root/rsync-options.txt"

"$PYTHON_BIN" "$STAGE_HELPER" \
    --repo "$REPO_DIR" \
    --ref "$ref" \
    --manifest "$DEPLOY_MANIFEST" \
    --stage-dir "$stage_dir" \
    --metadata-out "$metadata_file" >/dev/null \
    || die "failed to build immutable deployment stage"

mapfile -t metadata_values < <(
    "$PYTHON_BIN" - "$metadata_file" <<'PY'
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
fields = ("commit", "manifest_sha256", "tree_sha256", "file_count")
if set(fields) - payload.keys():
    raise SystemExit("stage metadata is incomplete")
if re.fullmatch(r"[0-9a-f]{40}", str(payload["commit"])) is None:
    raise SystemExit("stage commit is invalid")
for field in ("manifest_sha256", "tree_sha256"):
    if re.fullmatch(r"[0-9a-f]{64}", str(payload[field])) is None:
        raise SystemExit(f"stage {field} is invalid")
if not isinstance(payload["file_count"], int) or payload["file_count"] <= 0:
    raise SystemExit("stage file_count is invalid")
for field in fields:
    print(payload[field])
PY
) || die "failed to validate stage metadata"
[[ "${#metadata_values[@]}" -eq 4 ]] || die "stage metadata did not contain four values"
readonly commit="${metadata_values[0]}"
readonly manifest_sha256="${metadata_values[1]}"
readonly tree_sha256="${metadata_values[2]}"
readonly file_count="${metadata_values[3]}"

[[ -f "$stage_dir/$SENTINEL_NAME" ]] || die "immutable stage is missing its sentinel"
[[ "$(<"$stage_dir/$SENTINEL_NAME")" == "$SENTINEL_VALUE" ]] \
    || die "immutable stage sentinel content does not match"

ssh "$REMOTE_HOST" sh -s -- "$REMOTE_DIR" "$SENTINEL_NAME" "$SENTINEL_VALUE" \
    >"$remote_output" <<'REMOTE'
set -eu
requested=$1
sentinel_name=$2
expected_sentinel=$3
target=$(readlink -f -- "$requested")
test -n "$target" && test "$target" != /
test -d "$target"
test -f "$target/$sentinel_name"
test "$(wc -l < "$target/$sentinel_name" | tr -d ' ')" = 1
test "$(cat -- "$target/$sentinel_name")" = "$expected_sentinel"
printf '%s\n' "$target"
REMOTE

if grep -q $'\r' "$remote_output"; then
    die "remote target response contains carriage returns"
fi
mapfile -t remote_lines <"$remote_output"
[[ "${#remote_lines[@]}" -eq 1 ]] || die "remote target response must contain exactly one line"
readonly remote_root="${remote_lines[0]}"
[[ "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || die "resolved remote root is not a safe absolute path"
[[ "$remote_root" != "/" ]] || die "resolved remote root cannot be filesystem root"
readonly remote_target="${REMOTE_HOST}:${remote_root%/}/"

rsync_args=(
    -a
    --human-readable
    --itemize-changes
    --delete-delay
    --filter='P /.git/***'
    --filter='P /.venv/***'
    --filter='P /venv/***'
    --filter='P /.env'
    --filter='P /.env.*'
    --filter='P /.local_archive/***'
    --filter='P /bot.log*'
    --filter='P /config/config.json'
    --filter='P /config/secrets.json'
    --filter='P /logs/***'
    --filter='P /data/***'
    --filter='P /plugins/*/data/***'
    --filter='P /plugins/*/cache/***'
    --filter='P /plugins/*/config.json'
    --filter='P /plugins/*/secrets.json'
    --filter='P /plugins/*/test_reports/***'
    --filter='P /plugins/pendo/test_tools/***'
    --filter='P /plugins/xiaoqing_chat/figures/***'
    --filter='P /plugins/arxiv_filter/best_model*/***'
    --filter='P /plugins/arxiv_filter/train_model/**/cache/***'
    --filter='P /plugins/arxiv_filter/train_model/arxiv_papers_with_abstract.csv'
    --filter='P /plugins/**/*.db'
    --filter='P /plugins/**/*.db-*'
    --filter='P /plugins/**/*.sqlite'
    --filter='P /plugins/**/*.sqlite-*'
    --filter='P /plugins/**/*.sqlite3'
    --filter='P /plugins/**/*.sqlite3-*'
)
printf '%s\n' "${rsync_args[@]}" >"$options_file"

printf 'Commit:      %s\n' "$commit"
printf 'Manifest:    sha256:%s\n' "$manifest_sha256"
printf 'Stage tree:  sha256:%s (%s files)\n' "$tree_sha256" "$file_count"
printf 'Destination: %s\n' "$remote_target"
printf 'Mode:        %s\n\n' "$mode"

printf '%s\n' '--- rsync dry-run (this exact plan is hashed below) ---'
rsync "${rsync_args[@]}" --dry-run "$stage_dir/" "$remote_target" | tee "$dry_run_plan"

plan_sha256="$({
    "$PYTHON_BIN" - \
        "$PLAN_SCHEMA" "$commit" "$manifest_sha256" "$tree_sha256" \
        "$REMOTE_HOST" "$remote_root" "$options_file" "$dry_run_plan" <<'PY'
import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
for value in sys.argv[1:7]:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
for filename in sys.argv[7:]:
    payload = Path(filename).read_bytes()
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
print(digest.hexdigest())
PY
} )" || die "failed to calculate plan digest"
readonly plan_sha256
[[ "$plan_sha256" =~ ^[0-9a-f]{64}$ ]] || die "calculated plan digest is invalid"
printf '\nPlan SHA256: %s\n' "$plan_sha256"

if [[ "$mode" == "preview" ]]; then
    printf 'Preview complete. Apply only this plan with:\n'
    printf '  %q --apply --confirm-delete --ref %s --expect-plan %s\n' \
        "$0" "$commit" "$plan_sha256"
    exit 0
fi

[[ "${expected_plan,,}" == "$plan_sha256" ]] \
    || die "dry-run plan digest does not match --expect-plan; preview again"

printf '%s\n' '--- applying the verified immutable plan with delete-delay ---'
rsync "${rsync_args[@]}" "$stage_dir/" "$remote_target"
printf 'Deployment completed from commit %s with plan %s.\n' "$commit" "$plan_sha256"
