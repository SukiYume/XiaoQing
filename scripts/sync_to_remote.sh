#!/usr/bin/env bash
# 预览或同步当前 XiaoQing 工作树到生产环境。

set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"

# 生产目标集中写在这里，按用户习惯直接在脚本中切换 secondary-production-host / production-host；
# 不把主机名、Python 或 Conda 环境变成额外命令行参数。
readonly REMOTE_HOST="production-host"
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
    "scripts/stop-bot.vbs"
    "scripts/run-bot-monitor.ps1"
    "scripts/run_process_with_rotating_logs.py"
    "${ARXIV_MODEL_FILES[@]}"
)

usage() {
    cat <<'USAGE'
用法：
  ./scripts/sync_to_remote.sh [--dry-run]
  ./scripts/sync_to_remote.sh --apply --confirm-delete

默认只预览。--apply 会同步当前工作树和 arXiv 运行权重，并删除远端过期源码。

生产配置、密钥、Minecraft 连接配置、日志和运行数据始终保留。
脚本不会停止或启动生产进程。

如需切换目标，请直接修改脚本顶部的 REMOTE_HOST 和 REMOTE_DIR。
USAGE
}

die() {
    printf 'sync_to_remote.sh: %s\n' "$*" >&2
    exit 1
}

# Git Bash/Linux 通常提供 sha256sum，macOS 默认提供 shasum；OpenSSL 作为
# 最后回退。统一只输出小写摘要，供同步后的远端逐文件校验使用。
sha256_file() {
    local digest output
    local file_path="$1"

    if command -v sha256sum >/dev/null 2>&1; then
        output="$(sha256sum "$file_path")" || return
        digest="${output%% *}"
    elif command -v shasum >/dev/null 2>&1; then
        output="$(shasum -a 256 "$file_path")" || return
        digest="${output%% *}"
    elif command -v openssl >/dev/null 2>&1; then
        output="$(openssl dgst -sha256 "$file_path")" || return
        digest="${output##* }"
    else
        return 127
    fi

    [[ "$digest" =~ ^[[:xdigit:]]{64}$ ]] || return 1
    printf '%s' "$digest" | tr '[:upper:]' '[:lower:]'
    printf '\n'
}

# ---------- 参数解析与本地门禁 ----------

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
[[ "$REMOTE_HOST" != -* && "$REMOTE_HOST" =~ ^[A-Za-z0-9_.@:-]+$ ]] \
    || die "unsafe remote host"
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ && "$REMOTE_DIR" != "/" ]] \
    || die "remote directory must be a safe non-root absolute path"
[[ "$ARXIV_MODEL_DIR" =~ ^[A-Za-z0-9._/-]+$ \
    && "$ARXIV_MODEL_DIR" != /* \
    && "$ARXIV_MODEL_DIR" != *".."* ]] \
    || die "arXiv model directory must be a safe repository-relative path"
command -v "$SSH_BIN" >/dev/null || die "ssh is required"
command -v "$RSYNC_BIN" >/dev/null || die "rsync is required"

required_checksums=()
for required_file in "${REMOTE_REQUIRED_FILES[@]}"; do
    local_file="$REPO_DIR/$required_file"
    [[ -f "$local_file" && ! -L "$local_file" && -s "$local_file" ]] \
        || die "required release file must be a non-empty regular file: $required_file"
    if [[ "$mode" == "apply" ]]; then
        checksum="$(sha256_file "$local_file")" \
            || die "cannot calculate SHA-256 for required release file: $required_file"
        required_checksums+=("$required_file" "$checksum")
    fi
done

# ---------- 远端根目录门禁 ----------

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

# ---------- rsync 规则与执行 ----------

# `P` 只保护接收端删除，不能阻止同名本地文件上传。这里使用 `-` 排除规则，
# 在未启用 --delete-excluded 时同时做到“发送端不上传、接收端不删除”。显式
# 规则放在 .gitignore 之前，确保生产运行态目录不受本地忽略文件变化影响。
rsync_args=(
    -a
    --checksum
    --human-readable
    --itemize-changes
    --delete-delay
    --delay-updates
    --partial
    --filter='- /.git/***'
    --filter='- /config/config.json'
    --filter='- /config/secrets.json'
    --filter='- /plugins/minecraft/config.json'
    --filter='- /logs/***'
    --filter='- /test_reports/runs/***'
    --filter='- /data/***'
    --filter='- /plugins/*/data/***'
    --filter='- /plugins/*/cache/***'
    --filter='- /plugins/*/backups/***'
    --filter='- /plugins/*/exports/***'
    --include='/.env.example'
    --include='/.env.*.example'
    --include="/$ARXIV_MODEL_DIR/***"
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

# ---------- 同步后完整性校验 ----------

"$SSH_BIN" "$REMOTE_HOST" sh -s -- \
    "$remote_root" "${required_checksums[@]}" <<'REMOTE'
set -eu
target=$1
shift
cd "$target"

sha256_file() {
    file_path=$1
    if command -v sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "$file_path") || return
        digest=${output%% *}
    elif command -v shasum >/dev/null 2>&1; then
        output=$(shasum -a 256 "$file_path") || return
        digest=${output%% *}
    elif command -v openssl >/dev/null 2>&1; then
        output=$(openssl dgst -sha256 "$file_path") || return
        digest=${output##* }
    else
        return 127
    fi
    printf '%s' "$digest" | tr '[:upper:]' '[:lower:]'
    printf '\n'
}

while test "$#" -gt 0; do
    test "$#" -ge 2 || {
        printf 'invalid remote verification arguments\n' >&2
        exit 1
    }
    required_file=$1
    expected_checksum=$2
    shift 2

    case "$required_file" in
        /*|*..*)
            printf 'unsafe remote release path: %s\n' "$required_file" >&2
            exit 1
            ;;
    esac
    if ! test -f "$required_file" || test -L "$required_file" || ! test -s "$required_file"; then
        printf 'remote release file is not a non-empty regular file: %s\n' "$required_file" >&2
        exit 1
    fi
    actual_checksum=$(sha256_file "$required_file") || {
        printf 'cannot calculate remote SHA-256: %s\n' "$required_file" >&2
        exit 1
    }
    if test "$actual_checksum" != "$expected_checksum"; then
        printf 'remote release checksum mismatch: %s\n' "$required_file" >&2
        exit 1
    fi
done
REMOTE

printf 'Sync complete; required remote code and arXiv model SHA-256 checks passed.\n'
printf 'Production processes were not stopped or started.\n'
