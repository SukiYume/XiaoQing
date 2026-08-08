#!/usr/bin/env bash
# 仅清理仓库源码区生成的 Python/测试缓存，不进入插件运行数据目录。

set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"

case "$REPO_ROOT" in
    /|"")
        echo "Refusing to clean an unsafe repository root: $REPO_ROOT" >&2
        exit 2
        ;;
esac

if [ ! -f "$REPO_ROOT/.xiaoqing-sync-root" ] || \
   [ ! -f "$REPO_ROOT/pyproject.toml" ] || \
   [ ! -f "$REPO_ROOT/main.py" ] || \
   [ ! -d "$REPO_ROOT/core" ]; then
    echo "Refusing to clean: XiaoQing repository sentinels are missing under $REPO_ROOT" >&2
    exit 2
fi

printf 'Cleaning Python cache files under %s ...\n' "$REPO_ROOT"

# GNU/Git Bash find 使用 -xdev，macOS/BSD find 使用前置 -x。两者都不可用时
# 仍不跟随目录符号链接，并明确告警；仓库根和删除类型仍由其余门禁约束。
find_style="none"
if command find "$REPO_ROOT" -xdev -prune >/dev/null 2>&1; then
    find_style="gnu"
elif command find -x "$REPO_ROOT" -prune >/dev/null 2>&1; then
    find_style="bsd"
else
    printf 'WARNING: find does not expose a same-filesystem option; continuing without it\n' >&2
fi

find_repo() {
    case "$find_style" in
        gnu) command find "$REPO_ROOT" -xdev "$@" ;;
        bsd) command find -x "$REPO_ROOT" "$@" ;;
        *) command find "$REPO_ROOT" "$@" ;;
    esac
}

# 本地归档、虚拟环境、日志和新旧两套插件运行数据都保留；测试报告继续扫描，
# 但只会删除明确列出的 Python/测试缓存类型。
readonly -a PRUNE_EXPRESSION=(
    '('
    -path "$REPO_ROOT/.git"
    -o -path "$REPO_ROOT/.local_archive"
    -o -path "$REPO_ROOT/.venv"
    -o -path "$REPO_ROOT/venv"
    -o -path "$REPO_ROOT/data"
    -o -path "$REPO_ROOT/logs"
    -o -path "$REPO_ROOT/plugins/*/data"
    -o -path "$REPO_ROOT/plugins/*/cache"
    -o -path "$REPO_ROOT/plugins/*/backups"
    -o -path "$REPO_ROOT/plugins/*/exports"
    ')'
    -prune
    -o
)

find_repo "${PRUNE_EXPRESSION[@]}" \
    -type d \
    \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".pytest_tmp" -o -name ".ruff_cache" -o -name ".mypy_cache" \) \
    -prune -exec rm -rf -- {} +
find_repo "${PRUNE_EXPRESSION[@]}" \
    -type f \( -name "*.pyc" -o -name "*.pyo" \) -exec rm -f -- {} +
rm -f -- "$REPO_ROOT/.coverage"

printf 'Done! Python cache files cleaned.\n'
