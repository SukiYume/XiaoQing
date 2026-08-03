"""测试临时仓库使用的最小 Git 命令工具。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_TRANSIENT_LOCK_MARKERS = (
    "index.lock",
    "could not lock",
    "unable to create temporary file",
    "unable to write new index file",
    "resource temporarily unavailable",
)
_TRANSIENT_RETRY_DELAYS = (0.02, 0.05)
_MAX_DIAGNOSTIC_CHARS = 2_000


def _bounded_text(value: str, limit: int) -> str:
    """在固定预算内保留诊断首尾，避免只留重复噪声或丢掉最终错误。"""

    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    head = (limit - 1) // 2
    tail = limit - head - 1
    return f"{value[:head]}…{value[-tail:]}"


def _isolated_git_env() -> dict[str, str]:
    """移除调用方的临时索引，确保命令只操作传入的测试仓库。"""

    env = os.environ.copy()
    env.pop("GIT_INDEX_FILE", None)
    return env


class GitCommandError(RuntimeError):
    """携带有限 stdout/stderr 的临时 Git 仓库命令错误。"""

    def __init__(
        self,
        repo: Path,
        args: tuple[str, ...],
        result: subprocess.CompletedProcess[str],
    ) -> None:
        self.repo = repo
        self.git_args = args
        self.result = result
        header = f"git command failed with exit code {result.returncode}"
        repo_prefix = "; repo="
        repo_budget = _MAX_DIAGNOSTIC_CHARS - len(header) - len(repo_prefix)
        message = f"{header}{repo_prefix}{_bounded_text(str(repo), repo_budget)}"
        for label, value in (
            ("stderr", (result.stderr or "").strip()),
            ("stdout", (result.stdout or "").strip()),
        ):
            if not value:
                continue
            field_prefix = f"; {label}="
            remaining = _MAX_DIAGNOSTIC_CHARS - len(message) - len(field_prefix)
            if remaining <= 0:
                break
            message = f"{message}{field_prefix}{_bounded_text(value, remaining)}"
        super().__init__(message)


def _is_transient_lock_failure(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 128:
        return False
    diagnostic = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return any(marker in diagnostic for marker in _TRANSIENT_LOCK_MARKERS)


def run_git(repo: Path, *args: str) -> str:
    """运行 Git；仅对明确的临时索引锁竞争做两次短退避重试。"""

    command = ["git", "-C", str(repo), *args]
    for attempt in range(len(_TRANSIENT_RETRY_DELAYS) + 1):
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_isolated_git_env(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if attempt >= len(_TRANSIENT_RETRY_DELAYS) or not _is_transient_lock_failure(result):
            raise GitCommandError(repo, args, result)
        time.sleep(_TRANSIENT_RETRY_DELAYS[attempt])
    raise AssertionError("unreachable Git retry state")
