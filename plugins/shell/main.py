"""仅供管理员执行本地命令的无状态插件。

管理员身份和入站认证是权限边界；命令启用列表及受限模式只用于减少误触，
不构成解释器、参数、网络或子进程沙箱。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import ntpath
import os
import re
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import head_tail_preview
from core.plugin_base import segments as _core_segments
from core.public_errors import public_error_response
from core.sensitive_audit import log_sensitive_operation, safe_audit_id

from .config import (
    DANGEROUS_PATTERNS,
    DEFAULT_TIMEOUT,
    DEFAULT_WHITELIST,
    MAX_OUTPUT_LENGTH,
    UNSUPPORTED_SHELL_BUILTINS,
)

logger = logging.getLogger(__name__)

MessageSegments = list[dict[str, Any]]
segments = cast(Callable[[Any], MessageSegments], _core_segments)

_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_DANGEROUS_REGEXES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in DANGEROUS_PATTERNS)
_MAX_CAPTURE_BYTES = max(64 * 1024, MAX_OUTPUT_LENGTH * 4)
_PROCESS_STOP_TIMEOUT = 5.0
_EXIT_TIMEOUT = -1
_EXIT_OUTPUT_LIMIT = -2


class ShellContext(Protocol):
    """Shell 入口实际依赖的最小上下文。"""

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


def init(context: object | None = None) -> None:
    """记录插件初始化完成。"""

    logger.info("Shell plugin initialized")


def _audit_request_id(context: object) -> str:
    value = getattr(context, "request_id", "")
    candidate = value if isinstance(value, str) else ""
    return safe_audit_id(candidate)


def _log_command_audit(
    context: object,
    command_text: str,
    *,
    status: str,
    return_code: int | None = None,
    exc: BaseException | None = None,
) -> None:
    log_sensitive_operation(
        logger,
        "shell.execute",
        request_id=_audit_request_id(context),
        status=status,
        return_code=return_code,
        payload=command_text,
        exc=exc,
    )


def _get_config(context: ShellContext) -> Mapping[str, Any]:
    """从当前原子设置代取得插件密钥配置。"""

    return context.get_settings_snapshot().plugin_secrets("shell")


def _normalize_command_name(value: str) -> str:
    """统一路径、大小写和 Windows 可执行文件扩展名。"""

    name = re.split(r"[\\/]", value.strip())[-1].casefold()
    if sys.platform == "win32" and name.endswith(".exe"):
        return name[:-4]
    return name


def _normalize_whitelist_entries(value: object) -> set[str]:
    """只接受命令名序列，忽略空值和非字符串配置。"""

    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    entries: set[str] = set()
    for item in value:
        if isinstance(item, str):
            normalized = _normalize_command_name(item)
            if normalized:
                entries.add(normalized)
    return entries


def _get_whitelist(context: ShellContext) -> set[str]:
    """按 replace/extend 模式构造管理员命令启用列表。"""

    config = _get_config(context)
    if "whitelist" not in config:
        return set(DEFAULT_WHITELIST)

    custom_set = _normalize_whitelist_entries(config.get("whitelist"))
    mode = config.get("whitelist_mode", "replace")
    if mode == "extend":
        custom_set.update(DEFAULT_WHITELIST)
    return custom_set - UNSUPPORTED_SHELL_BUILTINS


def _get_timeout(context: ShellContext) -> float:
    """返回有限正超时；无效配置回退到默认值。"""

    candidate = _get_config(context).get("timeout", DEFAULT_TIMEOUT)
    if isinstance(candidate, bool):
        return float(DEFAULT_TIMEOUT)
    try:
        timeout = float(candidate)
    except (TypeError, ValueError, OverflowError):
        return float(DEFAULT_TIMEOUT)
    return timeout if math.isfinite(timeout) and timeout > 0 else float(DEFAULT_TIMEOUT)


def _is_whitelist_disabled(context: ShellContext) -> bool:
    """仅把 JSON 布尔值 true 视为显式关闭启用列表。"""

    return _get_config(context).get("disable_whitelist") is True


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _looks_like_path(token: str) -> bool:
    if not token:
        return False
    if _URL_SCHEME_RE.match(token):
        return False
    if token.startswith("-"):
        return False
    if sys.platform == "win32":
        if _WINDOWS_DRIVE_PATH_RE.match(token):
            return True
        if token.startswith("//"):
            return True
        if token.startswith(("./", "../", "~/")):
            return True
        if token.startswith("/") and not token.startswith("//"):
            return False
        return "/" in token or "\\" in token
    return token.startswith(("/", "./", "../", "~/"))


def _normalize_path_token(token: str) -> str:
    if "=" in token:
        key, value = token.split("=", 1)
        value = _strip_outer_quotes(value)
        if key and _looks_like_path(value):
            return f"{key}={_normalize_path_token(value)}"
        return f"{key}={value}"

    if not _looks_like_path(token):
        return token

    if sys.platform == "win32":
        if token.startswith("~/"):
            token = os.path.expanduser(token)
        return ntpath.normpath(token)

    if token.startswith("~/"):
        return os.path.expanduser(token)
    return token


def _merge_windows_quoted_assignments(parts: list[str]) -> list[str] | None:
    """合并 ``key="含空格值"`` 被非 POSIX shlex 拆开的片段。"""

    merged: list[str] = []
    pending: list[str] = []
    for part in parts:
        if pending:
            pending.append(part)
            if part.count('"') % 2 == 1:
                merged.append(" ".join(pending))
                pending = []
            continue
        _, separator, value = part.partition("=")
        if separator and value.count('"') % 2 == 1:
            pending = [part]
            continue
        merged.append(part)
    return None if pending else merged


def _split_command(cmd_line: str) -> list[str] | None:
    """安全拆分命令参数，并按运行系统规范化路径参数。"""
    try:
        parts = shlex.split(cmd_line, posix=sys.platform != "win32")
    except ValueError:
        return None
    if sys.platform == "win32":
        merged_parts = _merge_windows_quoted_assignments(parts)
        if merged_parts is None:
            return None
        parts = merged_parts
    parts = [_normalize_path_token(_strip_outer_quotes(part)) for part in parts]
    return parts if parts else None


def _validate_command(
    cmd_line: str,
    context: ShellContext,
    *,
    parsed_args: list[str] | None = None,
) -> str | None:
    """验证命令文本和入口名称，返回拒绝原因或 ``None``。"""

    if not cmd_line.strip():
        return "命令不能为空"

    for pattern in _DANGEROUS_REGEXES:
        if pattern.search(cmd_line):
            return f"包含受限的危险模式: {pattern.pattern}"

    if parsed_args is None:
        parsed_args = _split_command(cmd_line)
    cmd_name = _normalize_command_name(parsed_args[0]) if parsed_args else None
    if not cmd_name:
        return "无法解析命令"

    if cmd_name in UNSUPPORTED_SHELL_BUILTINS:
        return f"命令 '{cmd_name}' 依赖 shell 内建语义，不能直接执行"

    if not _is_whitelist_disabled(context):
        whitelist = _get_whitelist(context)
        if cmd_name not in whitelist:
            return f"命令 '{cmd_name}' 未在管理员命令启用列表中"

    return None


def _smart_decode(data: bytes) -> str:
    """优先保留 UTF-8；Windows 本地命令再回退到 GBK。"""

    if not data:
        return ""

    encodings = ("utf-8", "gbk") if sys.platform == "win32" else ("utf-8",)
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("latin-1")


def _subprocess_group_kwargs() -> dict[str, Any]:
    """让每条命令拥有可整体终止的独立进程组。"""

    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creationflags} if creationflags else {}
    return {"start_new_session": True}


async def _wait_for_process_exit(
    proc: asyncio.subprocess.Process,
    timeout: float = _PROCESS_STOP_TIMEOUT,
) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (TimeoutError, OSError):
        return False
    return True


async def _kill_process_directly(proc: asyncio.subprocess.Process) -> None:
    """尽力终止单个进程，并为回收设置有限等待。"""

    if proc.returncode is None:
        with contextlib.suppress(OSError, AttributeError):
            proc.kill()
    await _wait_for_process_exit(proc)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """终止命令进程树；平台工具失败时回退到直接终止子进程。"""

    if proc.returncode is not None:
        return

    if sys.platform == "win32":
        killer: asyncio.subprocess.Process | None = None
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.communicate(), timeout=_PROCESS_STOP_TIMEOUT)
            if killer.returncode == 0 and await _wait_for_process_exit(proc):
                return
        except Exception:
            await _kill_process_directly(proc)
            return
        finally:
            if killer is not None and killer.returncode is None:
                await _kill_process_directly(killer)
        await _kill_process_directly(proc)
        return

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        if not await _wait_for_process_exit(proc):
            await _kill_process_directly(proc)
        return
    except OSError:
        await _kill_process_directly(proc)
        return
    if not await _wait_for_process_exit(proc):
        await _kill_process_directly(proc)


async def _execute_command(args: list[str], timeout: float) -> tuple[int, str, str]:
    """异步执行命令，返回 ``(返回码, stdout, stderr)``。"""

    if not args:
        raise ValueError("command arguments must not be empty")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("command timeout must be finite and positive")

    proc = await asyncio.create_subprocess_exec(
        args[0],
        *args[1:],
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_subprocess_group_kwargs(),
    )

    overflow = asyncio.Event()
    captured_bytes = 0

    async def read_limited(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal captured_bytes
        if stream is None:
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            remaining = _MAX_CAPTURE_BYTES - captured_bytes
            if remaining > 0:
                chunks.append(chunk[:remaining])
                captured_bytes += min(len(chunk), remaining)
            if len(chunk) > remaining or captured_bytes >= _MAX_CAPTURE_BYTES:
                overflow.set()
                break
        return b"".join(chunks)

    stdout_task = asyncio.create_task(read_limited(proc.stdout))
    stderr_task = asyncio.create_task(read_limited(proc.stderr))
    wait_task = asyncio.create_task(proc.wait())
    overflow_task = asyncio.create_task(overflow.wait())
    status = 0
    message = ""

    try:
        done, _pending = await asyncio.wait(
            {wait_task, overflow_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if overflow_task in done and overflow.is_set():
            status = _EXIT_OUTPUT_LIMIT
            message = f"输出超过 {_MAX_CAPTURE_BYTES} 字节安全上限，已终止进程树"
            await _terminate_process_tree(proc)
        elif wait_task not in done:
            status = _EXIT_TIMEOUT
            message = f"命令执行超时（{timeout:g}秒）"
            await _terminate_process_tree(proc)

        stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
        if status == 0 and overflow.is_set():
            status = _EXIT_OUTPUT_LIMIT
            message = f"输出超过 {_MAX_CAPTURE_BYTES} 字节安全上限"
        if status == 0:
            status = proc.returncode or 0
        stderr_str = _smart_decode(stderr_bytes)
        if message:
            stderr_str = f"{stderr_str}\n{message}".strip()
        return status, _smart_decode(stdout_bytes), stderr_str
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(_terminate_process_tree(proc))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise
    finally:
        overflow_task.cancel()
        if proc.returncode is None:
            await _terminate_process_tree(proc)
        for task in (stdout_task, stderr_task, wait_task, overflow_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            stdout_task, stderr_task, wait_task, overflow_task, return_exceptions=True
        )


def _truncate(text: str, max_len: int = MAX_OUTPUT_LENGTH) -> str:
    """按首尾保留策略把文本严格限制在 ``max_len`` 个字符内。"""

    marker = "\n\n... 已省略中间内容 ...\n\n"
    return head_tail_preview(text, max_len, marker=marker)


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: ShellContext,
) -> MessageSegments:
    """处理帮助、启用列表查询和单条本地命令。"""

    try:
        cmd_line = args.strip()

        if not cmd_line:
            return segments(_show_help(context))

        cmd_args = _split_command(cmd_line)
        if not cmd_args:
            return segments("❌ 无法解析命令参数")

        first = cmd_args[0].casefold()
        if first in {"help", "帮助", "?", "-h", "--help"}:
            if len(cmd_args) != 1:
                return segments("❌ 用法: /shell help")
            return segments(_show_help(context))

        if first in {"list", "列表", "-l", "--list"}:
            if len(cmd_args) != 1:
                return segments("❌ 用法: /shell list")
            return _list_whitelist(context)

        error = _validate_command(cmd_line, context, parsed_args=cmd_args)
        if error:
            return segments(f"❌ 拒绝执行: {error}")

        _log_command_audit(context, cmd_line, status="started")
        timeout = _get_timeout(context)
        code, stdout, stderr = await _execute_command(cmd_args, timeout)

        streams = [("📤 stdout", stdout), ("⚠️ stderr", stderr)]
        populated_streams = [(label, text) for label, text in streams if text]
        output_parts: list[str] = []
        used_body_chars = 0
        for index, (label, text) in enumerate(populated_streams):
            remaining_streams = len(populated_streams) - index
            budget = max(0, (MAX_OUTPUT_LENGTH - used_body_chars) // remaining_streams)
            body = _truncate(text, budget)
            used_body_chars += len(body)
            output_parts.append(f"{label}:\n{body}")
        if not populated_streams:
            output_parts.append("(无输出)")

        status = "✅" if code == 0 else "❌"
        header = f"{status} 返回码: {code}\n"
        _log_command_audit(
            context,
            cmd_line,
            status="succeeded" if code == 0 else "failed",
            return_code=code,
        )
        return segments(header + "\n".join(output_parts))

    except Exception as exc:
        command_text = args if isinstance(args, str) else ""
        _log_command_audit(context, command_text, status="error", exc=exc)
        return cast(
            MessageSegments,
            public_error_response(
                context,
                exc,
                logger=logger,
                component="shell.handle",
            ),
        )


def _show_help(context: ShellContext) -> str:
    """根据当前配置生成帮助。"""

    whitelist_status = "已禁用" if _is_whitelist_disabled(context) else "已启用"
    timeout = _get_timeout(context)

    return (
        "💻 Shell 命令执行插件\n"
        "═══════════════════════\n\n"
        "📌 基本用法:\n\n"
        "1️⃣ /shell <命令>\n"
        "   执行终端命令\n\n"
        "2️⃣ /shell help\n"
        "   显示此帮助信息\n\n"
        "3️⃣ /shell list\n"
        "   查看管理员启用的命令入口\n\n"
        "🔧 管理员执行设置:\n"
        f"   • 命令启用/防误触列表: {whitelist_status}\n"
        f"   • 执行超时: {timeout:g}秒\n"
        f"   • 输出限制: {MAX_OUTPUT_LENGTH}字符\n"
        "   • 命令链接符: 已禁用\n\n"
        "💡 示例:\n"
        "   /shell ls -la\n"
        "   /shell pwd\n"
        "   /shell python --version\n"
        "   /shell ping -c 3 google.com\n\n"
        "📁 路径格式:\n"
        "   • QQ 中建议统一使用 / 斜杠，例如 C:/workspace/example.py\n"
        "   • 插件会按 bot 所在系统转换为本机路径格式\n"
        "   • 路径包含空格时请加引号\n\n"
        "⚠️ 注意: 此插件仅管理员可用；启用列表不是安全沙箱，解释器和通用工具仍具有管理员授予的完整能力\n"
        "═══════════════════════"
    )


def _list_whitelist(context: ShellContext) -> MessageSegments:
    """列出当前生效的管理员命令入口。"""

    if _is_whitelist_disabled(context):
        return segments("⚠️ 管理员命令启用列表已禁用；权限边界仍是 admin_only")

    whitelist = sorted(_get_whitelist(context))
    lines = ["管理员已启用的命令入口（防误触，不是安全沙箱）:"]
    lines.append(", ".join(whitelist))
    return segments("\n".join(lines))
