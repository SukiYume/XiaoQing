"""校验并规范化 Codex 任务工作目录。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import CodexPluginConfig

_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
MAX_CWD_CHARS = 32_768


class CwdError(ValueError):
    """请求的工作目录不合法或不在允许边界内。"""


def _resolve_absolute_path(raw: str, *, description: str) -> Path:
    if len(raw) > MAX_CWD_CHARS or any(ord(char) < 32 for char in raw):
        raise CwdError(f"{description}包含控制字符或超过长度上限。")
    if os.name != "nt" and _WINDOWS_ABS_RE.match(raw):
        raise CwdError("当前运行系统不是 Windows，不能直接使用 Windows 盘符路径。")
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise CwdError(f"{description}必须是绝对路径。")
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CwdError(f"{description}无法解析。") from exc


def normalize_cwd(raw_cwd: str | None, config: CodexPluginConfig) -> Path:
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        raise CwdError("工作目录必须是字符串。")
    raw = (raw_cwd or config.default_cwd).strip()
    if not raw:
        raw = config.default_cwd

    candidate = _resolve_absolute_path(raw, description="工作目录")
    allowed_roots = tuple(
        _resolve_absolute_path(item, description="允许根目录") for item in config.allowed_cwd_roots
    )
    if not allowed_roots:
        raise CwdError("未配置有效的工作目录允许范围。")

    # 必须先确认安全边界，再创建默认目录；否则错误配置会在允许根之外留下目录。
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        roots = "\n".join(f"- {root}" for root in allowed_roots)
        raise CwdError(f"工作目录不在允许范围内:\n{roots}")

    if raw_cwd is None:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CwdError("默认工作目录无法创建。") from exc

    if not candidate.exists() or not candidate.is_dir():
        raise CwdError(f"工作目录不存在: {candidate}")
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise CwdError("工作目录无法解析。") from exc
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        raise CwdError("工作目录在解析后越出允许范围。")

    return candidate
