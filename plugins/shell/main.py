"""
终端命令执行插件

仅管理员可用。包含以下安全措施：
1. 管理员命令启用列表（可配置，防误触而非安全沙箱）
2. 执行超时
3. 输出截断
4. 基本的命令注入防护

权限策略说明：
- 唯一安全边界是 Bot admin 权限与入站认证
- 命令启用列表仅用于减少误触，不能限制解释器、参数、网络或子进程能力
- 默认超时 30 秒
- 输出最大 4000 字符
- 禁止命令链接符（&&, ||, ;, |）除非在白名单
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
from typing import Any, Optional

from core.args import parse
from core.plugin_base import segments, text
from core.sensitive_audit import summarize_sensitive

logger = logging.getLogger(__name__)

URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
AUDIT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
AUDIT_STATUS_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
ERROR_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}\Z")

# 从配置文件导入常量
from .config import (
    DANGEROUS_PATTERNS,
    DEFAULT_TIMEOUT,
    DEFAULT_WHITELIST,
    MAX_OUTPUT_LENGTH,
    UNSUPPORTED_SHELL_BUILTINS,
)


def init(context=None) -> None:
    """插件初始化"""
    logger.info("Shell plugin initialized")


def _audit_request_id(context: object) -> str:
    value = getattr(context, "request_id", "")
    candidate = value if isinstance(value, str) else ""
    return candidate if AUDIT_ID_RE.fullmatch(candidate) else "-"


def _audit_error_type(exc: BaseException | None) -> str:
    if exc is None:
        return "-"
    candidate = type(exc).__name__
    return candidate if ERROR_TYPE_RE.fullmatch(candidate) else "Exception"


def _log_command_audit(
    context: object,
    command_text: str,
    *,
    status: str,
    return_code: int | None = None,
    exc: BaseException | None = None,
) -> None:
    summary = summarize_sensitive(command_text)
    error_type = _audit_error_type(exc)
    safe_status = status if AUDIT_STATUS_RE.fullmatch(status) else "unknown"
    log_method = logger.error if exc is not None else logger.info
    log_method(
        "sensitive_audit operation=shell.execute request_id=%s status=%s "
        "return_code=%s error_type=%s payload_kind=%s payload_length=%d "
        "payload_bytes=%d payload_fingerprint=%s",
        _audit_request_id(context),
        safe_status,
        return_code if return_code is not None else "-",
        error_type,
        summary.kind,
        summary.length,
        summary.byte_length,
        summary.fingerprint,
    )


# ============================================================
# 配置获取
# ============================================================


def _get_config(context) -> dict[str, Any]:
    """获取插件配置"""
    return context.secrets.get("plugins", {}).get("shell", {})


def _get_whitelist(context) -> set[str]:
    """
    获取命令白名单。

    支持两种模式（通过 secrets.json 的 whitelist_mode 配置）：
    - "replace": 完全替换默认白名单（默认行为）
    - "extend": 在默认白名单基础上追加自定义命令

    示例配置:
    {
        "plugins": {
            "shell": {
                "whitelist": ["custom_cmd1", "custom_cmd2"],
                "whitelist_mode": "extend"
            }
        }
    }
    """
    config = _get_config(context)
    custom_list = config.get("whitelist", [])
    mode = config.get("whitelist_mode", "replace")  # 默认为 replace 保持向后兼容

    if not custom_list:
        return DEFAULT_WHITELIST - UNSUPPORTED_SHELL_BUILTINS

    custom_set = set(custom_list)

    if mode == "extend":
        # 扩展模式：合并默认白名单和自定义命令
        return (DEFAULT_WHITELIST | custom_set) - UNSUPPORTED_SHELL_BUILTINS
    else:
        # 替换模式：仅使用自定义命令
        return custom_set - UNSUPPORTED_SHELL_BUILTINS


def _get_timeout(context) -> int:
    """获取执行超时"""
    config = _get_config(context)
    return int(config.get("timeout", DEFAULT_TIMEOUT))


def _is_whitelist_disabled(context) -> bool:
    """检查是否禁用白名单（危险）

    Note: Even when whitelist is disabled, DANGEROUS_PATTERNS blacklist
    is always enforced as a safety net.
    """
    config = _get_config(context)
    disabled = config.get("disable_whitelist", False)
    if disabled:
        logger.warning("Shell whitelist is disabled - DANGEROUS_PATTERNS blacklist still enforced")
    return disabled


# ============================================================
# 安全检查
# ============================================================


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _looks_like_path(token: str) -> bool:
    if not token:
        return False
    if URL_SCHEME_RE.match(token):
        return False
    if token.startswith("-"):
        return False
    if sys.platform == "win32":
        if WINDOWS_DRIVE_PATH_RE.match(token):
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
        if key and _looks_like_path(value):
            return f"{key}={_normalize_path_token(value)}"
        return token

    if not _looks_like_path(token):
        return token

    if sys.platform == "win32":
        if token.startswith("~/"):
            token = os.path.expanduser(token)
        return os.path.normpath(token)

    if token.startswith("~/"):
        return os.path.expanduser(token)
    return token


def _normalize_command_args(parts: list[str]) -> list[str]:
    return [_normalize_path_token(_strip_outer_quotes(part)) for part in parts]


def _split_command(cmd_line: str) -> Optional[list[str]]:
    """安全拆分命令参数，并按运行系统规范化路径参数。"""
    try:
        parts = shlex.split(cmd_line, posix=sys.platform != "win32")
    except ValueError:
        return None
    parts = _normalize_command_args(parts)
    return parts if parts else None


def _extract_command(cmd_line: str) -> Optional[str]:
    """提取命令名"""
    parts = _split_command(cmd_line)
    if not parts:
        return None
    return re.split(r"[\\/]", parts[0])[-1]


def _check_dangerous_patterns(cmd_line: str) -> Optional[str]:
    """检查危险模式"""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_line):
            return f"包含危险模式: {pattern}"
    return None


def _validate_command(cmd_line: str, context) -> Optional[str]:
    """
    验证命令是否可执行。

    返回:
        None 表示可以执行
        str 表示拒绝原因
    """
    if not cmd_line.strip():
        return "命令不能为空"

    # 检查危险模式
    danger = _check_dangerous_patterns(cmd_line)
    if danger:
        return danger

    # 提取命令名
    cmd_name = _extract_command(cmd_line)
    if not cmd_name:
        return "无法解析命令"

    if cmd_name in UNSUPPORTED_SHELL_BUILTINS:
        return f"命令 '{cmd_name}' 依赖 shell 内建语义，不能直接执行"

    # 白名单检查
    if not _is_whitelist_disabled(context):
        whitelist = _get_whitelist(context)
        if cmd_name not in whitelist:
            return f"命令 '{cmd_name}' 不在白名单中"

    return None


# ============================================================
# 命令执行
# ============================================================


def _smart_decode(data: bytes) -> str:
    """
    智能解码字节数据。

    Windows 中文系统命令输出通常是 GBK 编码，
    先尝试 GBK，失败则 fallback 到 UTF-8。
    """
    if not data:
        return ""

    # Windows 系统优先尝试 GBK
    if sys.platform == "win32":
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            pass

    # 尝试 UTF-8
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 最后使用 latin-1（不会失败）
    return data.decode("latin-1")


def _subprocess_group_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creationflags} if creationflags else {}
    return {"start_new_session": True}


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
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
            await asyncio.wait_for(killer.communicate(), timeout=5)
        except Exception:
            with contextlib.suppress(ProcessLookupError, ProcessLookupError, AttributeError):
                proc.kill()
                await proc.wait()
        return

    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()


async def _execute_command(args: list[str], timeout: int) -> tuple[int, str, str]:
    """
    异步执行命令。

    返回: (返回码, stdout, stderr)
    """
    proc = await asyncio.create_subprocess_exec(
        args[0],
        *args[1:],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_subprocess_group_kwargs(),
    )

    output_byte_limit = max(64 * 1024, MAX_OUTPUT_LENGTH * 4)
    overflow = asyncio.Event()

    async def read_limited(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            remaining = output_byte_limit - total
            if remaining > 0:
                chunks.append(chunk[:remaining])
                total += min(len(chunk), remaining)
            if len(chunk) > remaining or total >= output_byte_limit:
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
            status = -2
            message = f"输出超过 {output_byte_limit} 字节安全上限，已终止进程树"
            await _terminate_process_tree(proc)
        elif wait_task not in done:
            status = -1
            message = f"命令执行超时（{timeout}秒）"
            await _terminate_process_tree(proc)

        stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
        if status == 0:
            status = proc.returncode or 0
        stderr_str = _smart_decode(stderr_bytes)
        if message:
            stderr_str = f"{stderr_str}\n{message}".strip()
        return status, _smart_decode(stdout_bytes), stderr_str
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
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
    """截断输出"""
    if len(text) <= max_len:
        return text
    half = max_len // 2 - 20
    return text[:half] + f"\n\n... 省略 {len(text) - max_len} 字符 ...\n\n" + text[-half:]


# ============================================================
# 主处理函数
# ============================================================


async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        parsed = parse(args)
        cmd_line = args.strip()

        # 子命令路由
        if not parsed or not cmd_line:
            return segments(_show_help(context))

        first = parsed.first.lower()

        # 帮助命令
        if first in {"help", "帮助", "?", "-h", "--help"}:
            return segments(_show_help(context))

        # 列出白名单
        if first in {"list", "列表", "-l", "--list"}:
            return _list_whitelist(context)

        # 执行命令
        # 验证命令
        error = _validate_command(cmd_line, context)
        if error:
            return segments(f"❌ 拒绝执行: {error}")

        cmd_args = _split_command(cmd_line)
        if not cmd_args:
            return segments("❌ 无法解析命令参数")

        # 执行命令
        _log_command_audit(context, cmd_line, status="started")
        timeout = _get_timeout(context)

        code, stdout, stderr = await _execute_command(cmd_args, timeout)

        # 格式化输出
        output_parts = []
        if stdout:
            output_parts.append(f"📤 stdout:\n{_truncate(stdout)}")
        if stderr:
            output_parts.append(f"⚠️ stderr:\n{_truncate(stderr)}")
        if not output_parts:
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
        return segments(f"处理请求时出错: {str(exc)}")


def _show_help(context) -> str:
    """显示帮助信息"""
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
        "   查看允许的命令白名单\n\n"
        f"🔧 管理员执行设置:\n"
        f"   • 命令启用/防误触列表: {whitelist_status}\n"
        f"   • 执行超时: {timeout}秒\n"
        f"   • 输出限制: {MAX_OUTPUT_LENGTH}字符\n"
        f"   • 命令链接符: 已禁用\n\n"
        "💡 示例:\n"
        "   /shell ls -la\n"
        "   /shell pwd\n"
        "   /shell python --version\n"
        "   /shell ping -c 3 google.com\n\n"
        "📁 路径格式:\n"
        "   • QQ 中建议统一使用 / 斜杠，例如 C:/Users/testuser/Desktop/a.py\n"
        "   • 插件会按 bot 所在系统转换为本机路径格式\n"
        "   • 路径包含空格时请加引号\n\n"
        "⚠️ 注意: 此插件仅管理员可用；启用列表不是安全沙箱，解释器和通用工具仍具有管理员授予的完整能力\n"
        "═══════════════════════"
    )


def _list_whitelist(context) -> list[dict[str, Any]]:
    """列出白名单"""
    if _is_whitelist_disabled(context):
        return segments("⚠️ 管理员命令启用列表已禁用；权限边界仍是 admin_only")

    whitelist = sorted(_get_whitelist(context))
    lines = ["管理员已启用的命令入口（防误触，不是安全沙箱）:"]
    lines.append(", ".join(whitelist))
    return segments("\n".join(lines))
