"""管理员专用的有界 Jupyter 执行、内核管理和多轮代码缓冲入口。"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from core.interfaces import PluginContextProtocol
from core.plugin_base import Segments, has_control_characters, image_url, segments, text
from core.session import Session

from . import jupyter_manager
from .jupyter_audit import context_request_id, log_sensitive_audit
from .jupyter_config import (
    DEFAULT_TIMEOUT,
    MAX_CODE_BYTES,
    MAX_CODE_CHARS,
    MAX_EXECUTION_TIMEOUT,
    MAX_IMAGES,
    MAX_REPL_EXECUTIONS,
    MAX_REPL_LINE_CHARS,
    MAX_REPL_LINES,
    MAX_REPL_PREVIEW_CHARS,
    MAX_TOTAL_IMAGE_BYTES,
    MIN_EXECUTION_TIMEOUT,
    REPL_SESSION_TIMEOUT,
)
from .jupyter_models import ExecutionResult

# 保留稳定的测试/扩展接缝；依赖可用状态始终从模块属性实时读取。
JupyterKernelManager = jupyter_manager.JupyterKernelManager

logger = logging.getLogger(__name__)

PLUGIN_NAME = "jupyter"
MAX_KERNEL_ARGUMENT_CHARS = 64
_TIMEOUT_PREFIX_ALLOWANCE = 64

MainAction = Literal["execute", "help", "repl"]
KernelAction = Literal["status", "start", "restart", "shutdown", "help"]
ReplAction = Literal["run", "clear", "show", "help", "append"]

_MAIN_HELP_ALIASES = frozenset({"help", "帮助", "?"})
_REPL_ALIASES = frozenset({"repl", "interactive", "交互"})
_REPL_ACTIONS: dict[str, ReplAction] = {
    **dict.fromkeys({"run", "执行", "运行"}, "run"),
    **dict.fromkeys({"clear", "清空", "reset"}, "clear"),
    **dict.fromkeys({"show", "显示", "buffer", "缓冲区"}, "show"),
    **dict.fromkeys({"help", "帮助", "?"}, "help"),
}
_KERNEL_ACTIONS: dict[str, KernelAction] = {
    **dict.fromkeys({"status", "状态"}, "status"),
    **dict.fromkeys({"start", "启动"}, "start"),
    **dict.fromkeys({"restart", "重启"}, "restart"),
    **dict.fromkeys({"shutdown", "stop", "关闭", "停止"}, "shutdown"),
    **dict.fromkeys({"help", "帮助", "-h", "?"}, "help"),
}
_TIMEOUT_OPTION = re.compile(
    r"\A(?:-t|--timeout)(?:[ \t]+|=)([0-9]+(?:\.[0-9]+)?)(?:[ \t]|\Z)",
    re.IGNORECASE | re.ASCII,
)
_TIMEOUT_TOKEN = re.compile(
    r"\A(?:-t|--timeout)(?=[ \t=]|\Z)",
    re.IGNORECASE | re.ASCII,
)

HELP_TEXT = """📓 Jupyter 代码执行器

执行与 REPL
/py <代码>  在当前隔离内核执行代码
/py -t 60 <代码>  设置 0.1–600 秒超时
/py repl  启动 10 分钟代码缓冲会话
/py help  显示帮助

内核管理
/kernel status  查看内核状态
/kernel start  启动内核
/kernel restart  重启并清除变量
/kernel shutdown  关闭内核

代码最多 16000 字/32 KiB；输出和图片另有独立硬预算。内核空闲约 5 分钟后关闭。
"""

KERNEL_HELP_TEXT = """🔧 Jupyter 内核管理

/kernel status  查看状态
/kernel start  启动内核
/kernel restart  重启并清除变量
/kernel shutdown  关闭内核

内核按用户与群聊/私聊隔离，空闲约 5 分钟后自动关闭。
"""

REPL_HELP_TEXT = """
📝 Jupyter REPL 帮助
━━━━━━━━━━━━━━━━━━
💡 直接输入代码添加到缓冲区；若代码行恰好等于控制词，请在开头留一个空格
💡 输入「run」/「执行」运行代码
💡 输入「show」/「显示」查看缓冲区
💡 输入「clear」/「清空」清空缓冲区
💡 输入「退出」/「取消」结束会话
━━━━━━━━━━━━━━━━━━
""".strip()

DEPENDENCY_TEXT = '❌ Jupyter 依赖不可用；请运行 pip install "xiaoqing[jupyter]"'
OTHER_SESSION_TEXT = "⚠️ 当前已有其他插件会话；请先发送「退出」，再启动 Jupyter REPL。"
INVALID_SESSION_TEXT = "⚠️ Jupyter REPL 状态无效，已安全结束；请重新启动。"
_DEPENDENCY_PROBED = False


class JupyterCommandError(ValueError):
    """表示可直接反馈给管理员的输入边界错误。"""


class InvalidReplState(ValueError):
    """表示 REPL 会话不满足结构或资源不变量。"""


@dataclass(frozen=True, slots=True)
class ReplState:
    lines: tuple[str, ...]
    execution_count: int

    @property
    def code(self) -> str:
        return "\n".join(self.lines)


def _validate_code_text(value: object, *, allow_empty: bool) -> str:
    if type(value) is not str:
        raise TypeError("Jupyter code must be a string")
    if len(value) > MAX_CODE_CHARS or len(value.encode("utf-8")) > MAX_CODE_BYTES:
        raise JupyterCommandError(
            f"代码不能超过 {MAX_CODE_CHARS} 字或 {MAX_CODE_BYTES // 1024} KiB"
        )
    if has_control_characters(
        value,
        allow_formatting_whitespace=True,
        include_c1=True,
    ):
        raise JupyterCommandError("代码包含不允许的控制字符")
    if not allow_empty and not value.strip():
        raise JupyterCommandError("请输入要执行的代码")
    return value


def extract_code_and_timeout(args: object) -> tuple[str, float]:
    """只解析开头至多一个 timeout 选项，后续文本逐字作为代码。"""

    if type(args) is not str:
        raise TypeError("Jupyter code must be a string")
    if (
        len(args) > MAX_CODE_CHARS + _TIMEOUT_PREFIX_ALLOWANCE
        or len(args.encode("utf-8")) > MAX_CODE_BYTES + _TIMEOUT_PREFIX_ALLOWANCE
    ):
        raise JupyterCommandError(
            f"代码不能超过 {MAX_CODE_CHARS} 字或 {MAX_CODE_BYTES // 1024} KiB"
        )
    if has_control_characters(
        args,
        allow_formatting_whitespace=True,
        include_c1=True,
    ):
        raise JupyterCommandError("代码包含不允许的控制字符")

    raw = args.lstrip(" \t")
    timeout = DEFAULT_TIMEOUT
    match = _TIMEOUT_OPTION.match(raw)
    if match is not None:
        timeout = float(match.group(1))
        if not MIN_EXECUTION_TIMEOUT <= timeout <= MAX_EXECUTION_TIMEOUT:
            raise JupyterCommandError(
                f"执行超时必须在 {MIN_EXECUTION_TIMEOUT:g}–{MAX_EXECUTION_TIMEOUT:g} 秒之间"
            )
        code = raw[match.end() :]
    elif _TIMEOUT_TOKEN.match(raw) is not None:
        raise JupyterCommandError("timeout 选项缺少有效的 ASCII 数值")
    else:
        code = args
    return _validate_code_text(code, allow_empty=True), timeout


def _parse_main_action(args: object) -> MainAction:
    if type(args) is not str:
        raise TypeError("Jupyter arguments must be a string")
    normalized = args.strip().casefold()
    if normalized in _MAIN_HELP_ALIASES:
        return "help"
    if normalized in _REPL_ALIASES:
        return "repl"
    return "execute"


def _parse_kernel_action(args: object) -> KernelAction:
    if type(args) is not str:
        raise TypeError("Jupyter kernel arguments must be a string")
    if len(args) > MAX_KERNEL_ARGUMENT_CHARS:
        raise JupyterCommandError(f"内核命令参数不能超过 {MAX_KERNEL_ARGUMENT_CHARS} 个字符")
    if has_control_characters(args, include_c1=True):
        raise JupyterCommandError("内核命令不能包含控制字符")
    try:
        tokens = shlex.split(args, posix=True)
    except ValueError as exc:
        raise JupyterCommandError("内核命令中的引号没有闭合") from exc
    if not tokens:
        return "status"
    if len(tokens) != 1:
        raise JupyterCommandError("用法：/kernel [status|start|restart|shutdown|help]")
    action = _KERNEL_ACTIONS.get(tokens[0].casefold())
    if action is None:
        raise JupyterCommandError("未知内核操作；请使用 status、start、restart 或 shutdown")
    return action


def _dependencies_available() -> bool:
    global _DEPENDENCY_PROBED
    if not _DEPENDENCY_PROBED:
        jupyter_manager.lazy_import_jupyter()
        _DEPENDENCY_PROBED = True
    return bool(jupyter_manager.JUPYTER_AVAILABLE and jupyter_manager.KernelManager is not None)


def init(context: PluginContextProtocol | None = None) -> None:
    """在插件代加载时探测可选依赖，不启动任何内核。"""

    global _DEPENDENCY_PROBED
    _DEPENDENCY_PROBED = False
    available = _dependencies_available()
    log_sensitive_audit(
        logger,
        "jupyter.init",
        request_id=context_request_id(context),
        status="available" if available else "unavailable",
        error_type="-" if available else "ImportError",
        level=logging.INFO if available else logging.WARNING,
    )


def _owner_key(context: PluginContextProtocol) -> str:
    """拒绝缺失身份的共享全局内核，按用户与会话场景生成稳定键。"""

    user_id = context.current_user_id
    group_id = context.current_group_id
    if type(user_id) is not int or user_id <= 0:
        raise RuntimeError("Jupyter execution requires an exact positive user id")
    if group_id is None:
        return f"user:{user_id}:private"
    if type(group_id) is not int or group_id <= 0:
        raise RuntimeError("Jupyter execution requires an exact positive group id")
    return f"user:{user_id}:group:{group_id}"


def _build_result_segments(
    result: ExecutionResult,
    *,
    header: str | None = None,
    footer: str | None = None,
) -> Segments:
    response: Segments = []
    if header:
        response.append(text(header))
    response.append(text(f"```\n{result.format_output()}\n```"))

    total_image_bytes = 0
    for image_data in result.images[:MAX_IMAGES]:
        if (
            not jupyter_manager.validate_png_bytes(image_data)
            or total_image_bytes + len(image_data) > MAX_TOTAL_IMAGE_BYTES
        ):
            continue
        total_image_bytes += len(image_data)
        encoded = base64.b64encode(image_data).decode("ascii")
        response.append(image_url(f"base64://{encoded}"))
    if footer:
        response.append(text(footer))
    return response


async def _handle_execute(args: str, context: PluginContextProtocol) -> Segments:
    """执行一次有界代码请求；异常正文只进入安全审计的类型字段。"""

    code, timeout = extract_code_and_timeout(args)
    if not code.strip():
        return segments("请输入要执行的代码\n用法: /py print('hello')\n输入 /py help 查看帮助")
    job_id = uuid.uuid4().hex
    request_id = context_request_id(context)
    try:
        manager = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
        log_sensitive_audit(
            logger,
            "jupyter.execute",
            request_id=request_id,
            job_id=job_id,
            status="started",
            payload=code,
        )
        result = await manager.execute(code, timeout=timeout, audit_id=job_id)
        log_sensitive_audit(
            logger,
            "jupyter.execute",
            request_id=request_id,
            job_id=job_id,
            status="succeeded" if result.success else "failed",
            payload=code,
        )
        return _build_result_segments(result)
    except Exception as exc:
        log_sensitive_audit(
            logger,
            "jupyter.execute",
            request_id=request_id,
            job_id=job_id,
            status="error",
            payload=code,
            exc=exc,
        )
        return segments("❌ 执行失败，请稍后重试")


async def _handle_kernel(action: KernelAction, context: PluginContextProtocol) -> Segments:
    """执行已经过严格解析的内核状态或生命周期操作。"""

    job_id = uuid.uuid4().hex
    request_id = context_request_id(context)
    try:
        manager = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
        if action == "status":
            status = manager.get_status()
            marker = "🟢" if status["running"] else "⚫"
            return segments(f"{marker} {status['message']}")

        log_sensitive_audit(
            logger,
            f"jupyter.kernel.{action}",
            request_id=request_id,
            job_id=job_id,
            status="started",
        )
        if action == "start":
            await asyncio.to_thread(manager.start_kernel)
            manager.ensure_idle_monitor()
            success_text = "🟢 内核已启动"
        elif action == "restart":
            await asyncio.to_thread(manager.restart_kernel)
            manager.ensure_idle_monitor()
            success_text = "🔄 内核已重启"
        else:
            await asyncio.to_thread(manager.shutdown_kernel)
            success_text = "⚫ 内核已关闭"
        log_sensitive_audit(
            logger,
            f"jupyter.kernel.{action}",
            request_id=request_id,
            job_id=job_id,
            status="succeeded",
        )
        return segments(success_text)
    except Exception as exc:
        log_sensitive_audit(
            logger,
            f"jupyter.kernel.{action}",
            request_id=request_id,
            job_id=job_id,
            status="quarantined" if action == "shutdown" else "error",
            exc=exc,
        )
        if action == "shutdown":
            return segments("⚠️ 内核关闭状态无法确认，实例已隔离")
        return segments("❌ 内核操作失败，请稍后重试")


def _load_repl_state(session: Session) -> ReplState:
    raw_lines = session.get("code_buffer")
    execution_count = session.get("execution_count")
    if type(raw_lines) is not list or len(raw_lines) > MAX_REPL_LINES:
        raise InvalidReplState("code buffer shape is invalid")
    if type(execution_count) is not int or not 0 <= execution_count <= MAX_REPL_EXECUTIONS:
        raise InvalidReplState("execution count is invalid")

    lines: list[str] = []
    for line in raw_lines:
        if (
            type(line) is not str
            or "\n" in line
            or "\r" in line
            or len(line) > MAX_REPL_LINE_CHARS
            or has_control_characters(
                line,
                allow_formatting_whitespace=True,
                include_c1=True,
            )
        ):
            raise InvalidReplState("code buffer line is invalid")
        lines.append(line)
    code = "\n".join(lines)
    try:
        _validate_code_text(code, allow_empty=True)
    except (JupyterCommandError, TypeError) as exc:
        raise InvalidReplState("code buffer budget is invalid") from exc
    return ReplState(tuple(lines), execution_count)


def _code_preview(code: str) -> str:
    suffix = "\n…（预览已截断，执行仍使用完整缓冲区）"
    if len(code) <= MAX_REPL_PREVIEW_CHARS:
        return code
    kept = MAX_REPL_PREVIEW_CHARS - len(suffix)
    return f"{code[:kept]}{suffix}"


def _append_repl_text(state: ReplState, user_text: object) -> tuple[str, ...]:
    if type(user_text) is not str:
        raise TypeError("Jupyter REPL input must be a string")
    normalized = user_text.replace("\r\n", "\n").replace("\r", "\n")
    incoming = tuple(normalized.split("\n"))
    if len(state.lines) + len(incoming) > MAX_REPL_LINES:
        raise JupyterCommandError(f"REPL 缓冲区最多 {MAX_REPL_LINES} 行")
    if any(
        len(line) > MAX_REPL_LINE_CHARS
        or has_control_characters(
            line,
            allow_formatting_whitespace=True,
            include_c1=True,
        )
        for line in incoming
    ):
        raise JupyterCommandError(f"REPL 单行不能超过 {MAX_REPL_LINE_CHARS} 字，且不能包含控制字符")
    combined = (*state.lines, *incoming)
    _validate_code_text("\n".join(combined), allow_empty=True)
    return combined


async def _start_repl_session(context: PluginContextProtocol) -> Segments:
    """创建最小 REPL 状态；检测到其他插件会话时不读取其数据。"""

    existing = await context.get_session()
    if existing is not None:
        if type(existing.plugin_name) is not str or existing.plugin_name != PLUGIN_NAME:
            return segments(OTHER_SESSION_TEXT)
        try:
            state = _load_repl_state(existing)
        except InvalidReplState:
            await context.end_session()
            log_sensitive_audit(
                logger,
                "jupyter.repl.state",
                request_id=context_request_id(context),
                status="discarded",
                error_type="InvalidReplState",
                level=logging.WARNING,
            )
        else:
            tail = "\n".join(state.lines[-5:]) if state.lines else "（空）"
            return segments(
                "📝 你已在 Jupyter REPL 会话中\n"
                f"当前缓冲区（最后 5 行）:\n```python\n{_code_preview(tail)}\n```\n\n"
                "继续输入代码，或输入 run/show/clear；发送「退出」结束会话"
            )

    await context.create_session(
        initial_data={"code_buffer": [], "execution_count": 0},
        timeout=REPL_SESSION_TIMEOUT,
    )
    log_sensitive_audit(
        logger,
        "jupyter.repl.start",
        request_id=context_request_id(context),
        job_id=uuid.uuid4().hex,
        status="succeeded",
    )
    return segments(
        "📝 Jupyter REPL 已启动\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "直接输入代码；run 执行，show 查看，clear 清空\n"
        "发送「退出」/「取消」结束会话\n"
        "━━━━━━━━━━━━━━━━━━"
    )


async def _execute_repl_buffer(
    state: ReplState,
    session: Session,
    context: PluginContextProtocol,
) -> Segments:
    if not state.code.strip():
        return segments("⚠️ 缓冲区为空，请先输入代码")
    if state.execution_count >= MAX_REPL_EXECUTIONS:
        return segments("⚠️ REPL 执行计数已达上限，请退出后重新启动")

    job_id = uuid.uuid4().hex
    request_id = context_request_id(context)
    try:
        manager = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
        log_sensitive_audit(
            logger,
            "jupyter.repl.execute",
            request_id=request_id,
            job_id=job_id,
            status="started",
            payload=state.code,
        )
        result = await manager.execute(state.code, timeout=DEFAULT_TIMEOUT, audit_id=job_id)
        log_sensitive_audit(
            logger,
            "jupyter.repl.execute",
            request_id=request_id,
            job_id=job_id,
            status="succeeded" if result.success else "failed",
            payload=state.code,
        )
        if result.success:
            next_count = state.execution_count + 1
            session.set("code_buffer", [])
            session.set("execution_count", next_count)
            header = f"✅ 执行完成 (#{next_count})"
            footer = "继续输入代码，或发送「退出」结束会话"
        else:
            header = "⚠️ 执行未成功，缓冲区已保留"
            footer = "可修改后再次输入 run"
        return _build_result_segments(result, header=header, footer=footer)
    except Exception as exc:
        log_sensitive_audit(
            logger,
            "jupyter.repl.execute",
            request_id=request_id,
            job_id=job_id,
            status="error",
            payload=state.code,
            exc=exc,
        )
        return segments("❌ 执行失败，缓冲区已保留；请稍后重试")


async def handle_session(
    user_text: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
    session: Session,
) -> Segments:
    """在框架的单会话事务内处理 REPL 管理动作或追加代码。"""

    del event
    if type(session.plugin_name) is not str or session.plugin_name != PLUGIN_NAME:
        raise ValueError("Jupyter received a foreign session")
    try:
        state = _load_repl_state(session)
    except InvalidReplState:
        await context.end_session()
        return segments(INVALID_SESSION_TEXT)
    if type(user_text) is not str:
        raise TypeError("Jupyter REPL input must be a string")

    normalized_action = (
        user_text.strip().casefold()
        if "\n" not in user_text and not user_text[:1].isspace()
        else ""
    )
    action = _REPL_ACTIONS.get(normalized_action, "append")
    if action == "run":
        return await _execute_repl_buffer(state, session, context)
    if action == "clear":
        session.set("code_buffer", [])
        return segments("🗑️ 缓冲区已清空")
    if action == "show":
        if not state.lines:
            return segments("📄 缓冲区为空")
        return segments(
            f"📄 当前缓冲区 ({len(state.lines)} 行):\n"
            f"```python\n{_code_preview(state.code)}\n```\n\n"
            "输入 run 执行，clear 清空"
        )
    if action == "help":
        return segments(REPL_HELP_TEXT)

    try:
        lines = _append_repl_text(state, user_text)
    except JupyterCommandError as exc:
        return segments(str(exc))
    session.set("code_buffer", list(lines))
    return segments(f"✓ 已添加 (共 {len(lines)} 行)\n输入 run 执行，show 查看，clear 清空")


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """按清单规范命令名路由执行、REPL 和内核管理。"""

    del event
    try:
        if command == "jupyter":
            main_action = _parse_main_action(args)
            if main_action == "help":
                return segments(HELP_TEXT)
            if not _dependencies_available():
                return segments(DEPENDENCY_TEXT)
            if main_action == "repl":
                return await _start_repl_session(context)
            return await _handle_execute(args, context)
        if command == "jupyter_kernel":
            kernel_action = _parse_kernel_action(args)
            if kernel_action == "help":
                return segments(KERNEL_HELP_TEXT)
            if not _dependencies_available():
                return segments(DEPENDENCY_TEXT)
            return await _handle_kernel(kernel_action, context)
        return segments("未知 Jupyter 命令")
    except JupyterCommandError as exc:
        return segments(str(exc))
    except Exception as exc:
        log_sensitive_audit(
            logger,
            "jupyter.handle",
            request_id=context_request_id(context),
            status="error",
            exc=exc,
        )
        return segments("处理请求失败，请稍后重试")


async def shutdown(context: PluginContextProtocol) -> None:
    """插件卸载时等待所有空闲监视器与内核清理完成。"""

    try:
        if not _dependencies_available():
            return
        request_id = context_request_id(context)
        log_sensitive_audit(
            logger,
            "jupyter.shutdown",
            request_id=request_id,
            status="started",
        )
        await JupyterKernelManager.shutdown_all_async()
        log_sensitive_audit(
            logger,
            "jupyter.shutdown",
            request_id=request_id,
            status="succeeded",
        )
    except Exception as exc:
        log_sensitive_audit(
            logger,
            "jupyter.shutdown",
            request_id=context_request_id(context),
            status="error",
            exc=exc,
        )
