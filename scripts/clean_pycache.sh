#!/bin/bash
# 仅清理仓库源码区生成的 Python/测试缓存，不进入插件运行数据目录。

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)

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

echo "Cleaning Python cache files under $REPO_ROOT ..."

# find 默认不跟随目录符号链接，-xdev 也会阻止清理跨越仓库所在文件系统。
# 本地归档、虚拟环境和插件运行数据要保留；测试报告继续扫描，但只会删除缓存类型。
find "$REPO_ROOT" -xdev \
    \( -path "$REPO_ROOT/.git" \
       -o -path "$REPO_ROOT/.local_archive" \
       -o -path "$REPO_ROOT/.venv" \
       -o -path "$REPO_ROOT/venv" \
       -o -path "$REPO_ROOT/plugins/*/data" \
       -o -path "$REPO_ROOT/plugins/*/backups" \
       -o -path "$REPO_ROOT/plugins/*/exports" \) -prune -o \
    -type d \
    \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".pytest_tmp" -o -name ".ruff_cache" -o -name ".mypy_cache" \) \
    -prune -exec rm -rf -- {} +
find "$REPO_ROOT" -xdev \
    \( -path "$REPO_ROOT/.git" \
       -o -path "$REPO_ROOT/.local_archive" \
       -o -path "$REPO_ROOT/.venv" \
       -o -path "$REPO_ROOT/venv" \
       -o -path "$REPO_ROOT/plugins/*/data" \
       -o -path "$REPO_ROOT/plugins/*/backups" \
       -o -path "$REPO_ROOT/plugins/*/exports" \) -prune -o \
    -type f \( -name "*.pyc" -o -name "*.pyo" \) -exec rm -f -- {} +
rm -f -- "$REPO_ROOT/.coverage"

echo "Done! Python cache files cleaned."
