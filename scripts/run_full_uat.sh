#!/usr/bin/env bash

# Git Bash、macOS 和 Linux 的统一全量 UAT 入口。
# 复杂的配置回滚与跨平台进程信号由同目录 Python 执行器负责；本脚本只负责
# 可靠定位仓库并使用调用者当前环境中的 Python 原样转发参数。

set -Eeuo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd -- "$script_dir/.." && pwd -P)"

cd -- "$repo_root"

if ! command -v python >/dev/null 2>&1; then
    printf 'ERROR: 当前环境中找不到 python\n' >&2
    exit 2
fi

exec python "$script_dir/run_full_uat.py" "$@"
