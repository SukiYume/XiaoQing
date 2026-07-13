from __future__ import annotations

import os
import re
from pathlib import Path

from .config import CodexPluginConfig

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


class CwdError(ValueError):
    """Raised when a requested working directory is not allowed."""


def _normalize_for_compare(path: Path) -> str:
    resolved = str(path.resolve(strict=False))
    return os.path.normcase(resolved) if os.name == "nt" else resolved


def _is_relative_to(child: Path, parent: Path) -> bool:
    child_key = _normalize_for_compare(child)
    parent_key = _normalize_for_compare(parent)
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key.rstrip("\\/") + os.sep)


def normalize_cwd(raw_cwd: str | None, config: CodexPluginConfig) -> Path:
    raw = (raw_cwd or config.default_cwd).strip()
    if not raw:
        raw = config.default_cwd

    if os.name != "nt" and _WINDOWS_ABS_RE.match(raw):
        raise CwdError("当前运行系统不是 Windows，不能直接使用 Windows 盘符路径。")

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise CwdError("工作目录必须是绝对路径。")

    candidate = candidate.resolve(strict=False)
    if raw_cwd is None:
        candidate.mkdir(parents=True, exist_ok=True)

    if not candidate.exists() or not candidate.is_dir():
        raise CwdError(f"工作目录不存在: {candidate}")

    allowed_roots = [Path(item).expanduser().resolve(strict=False) for item in config.allowed_cwd_roots]
    if not any(_is_relative_to(candidate, root) for root in allowed_roots):
        roots = "\n".join(f"- {root}" for root in allowed_roots)
        raise CwdError(f"工作目录不在允许范围内:\n{roots}")

    return candidate
