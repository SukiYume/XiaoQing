"""处理添加服务器对话、远端命令任务和图片下载等多轮 SSH 会话。

退出命令由框架识别；框架删除会话前会调用本插件的 ``close_session``，
由它取消准确的后台 job 并断开 SSH 连接。
"""

import asyncio
import logging
import posixpath
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

from core.args import parse_int
from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.interfaces import ACTION_BYPASS_SINK_KEY
from core.plugin_base import build_action, image
from core.plugin_base import text as text_segment
from core.sensitive_audit import summarize_sensitive

from .audit import audit_error_type, audit_id, audit_request_id
from .config import (
    CANCEL_KEYWORDS,
    EXIT_CODE_INTERRUPTED,
    EXIT_CODE_TIMEOUT,
    MAX_HISTORY_LENGTH,
    STOP_KEYWORDS,
    SessionKeys,
)
from .message_formatter import format_server_added
from .output_relay import SSHOutputPolicy, SSHOutputRelay, SSHOutputSummary
from .path_resolver import (
    build_command,
    extract_cwd_from_output,
    is_cd_command,
    resolve_remote_path,
    strip_cwd_markers,
)
from .ssh_manager import SSHManager, get_manager
from .types import Context, MessageSegments, OneBotEvent, Session, segments
from .validators import (
    validate_command,
    validate_hostname,
    validate_port,
    validate_server_name,
    validate_username,
)

logger = logging.getLogger(__name__)

_SessionJobKey = tuple[Any, Any]


class _ServerDraft(TypedDict, total=False):
    """引导式添加会话逐步收集的服务器配置。"""

    name: str
    host: str
    port: int
    username: str
    auth_type: str
    key_path: str


_REQUIRED_DRAFT_FIELDS: dict[str, tuple[str, ...]] = {
    "name": (),
    "host": ("name",),
    "port": ("name", "host"),
    "username": ("name", "host", "port"),
    "auth_type": ("name", "host", "port", "username"),
    "password": ("name", "host", "port", "username", "auth_type"),
    "key_path": ("name", "host", "port", "username", "auth_type"),
}
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"})
_MAX_SHOWIMG_FILES = 5
_MAX_SHOWIMG_BYTES = 10 * 1024 * 1024
_MAX_SHOWIMG_PATTERN_CHARS = 512
_SHOWIMG_USAGE = (
    "用法: showimg <路径或通配符> [--page N]\n"
    "示例: showimg ./*.png 或 showimg ./plots/*.jpg --page 2"
)
_MAX_ENV_VARS = 64
_MAX_ENV_VALUE_CHARS = 4096
_MAX_ENV_TOTAL_CHARS = 32_768
_IMAGE_CACHE_LIMITS = FileCacheLimits(
    max_entries=64,
    max_bytes=64 * 1024 * 1024,
    ttl_seconds=3600,
)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_EXPORT_RE = re.compile(r"export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.ASCII)
_SENSITIVE_HISTORY_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"[A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|authorization|credential|"
    r"private[_-]?key)[A-Za-z0-9_]*\s*(?:=|:)"
)
_SENSITIVE_HISTORY_OPTION_RE = re.compile(
    r"(?i)(?:^|\s)--?(?:api[-_]?key|token|access[-_]?token|secret|password|passwd|"
    r"authorization|credential)(?:=|\s+)(?=\S)"
)
_SENSITIVE_HISTORY_HEADER_RE = re.compile(
    r"(?i)(?:^|[\s'\"])(?:authorization|proxy-authorization|x-api-key)\s*:"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
_SHOWIMG_PAGE_SUFFIX_RE = re.compile(r"\s+--page(?:\s+|=)(?P<page>\S+)\s*\Z", re.ASCII)
_SHOWIMG_PAGE_TOKEN_RE = re.compile(r"(?:^|\s)--page(?:\s|=|\Z)", re.ASCII)


@dataclass(frozen=True, slots=True)
class _ShowImageRequest:
    """一条已验证的图片匹配与分页请求。"""

    pattern: str
    page: int


class _ShowImageInputError(ValueError):
    """用户可修正的 showimg 参数错误。"""


def _is_showimg_command(value: str) -> bool:
    """识别保留的会话内图片命令，并接受空格或制表符分隔参数。"""

    parts = value.split(maxsplit=1)
    return bool(parts) and parts[0].casefold() == "showimg"


def _parse_showimg_request(value: str) -> _ShowImageRequest:
    """解析末尾 ``--page N``，同时保留路径中可见的空格。"""

    parts = value.strip().split(None, 1)
    if len(parts) < 2:
        raise _ShowImageInputError("缺少图片路径或通配符")

    raw_arguments = parts[1].strip()
    page = 1
    page_match = _SHOWIMG_PAGE_SUFFIX_RE.search(raw_arguments)
    if page_match is not None:
        parsed_page = parse_int(page_match.group("page"), minimum=1)
        if parsed_page is None:
            raise _ShowImageInputError("--page 页码必须是正整数")
        page = parsed_page
        file_pattern = raw_arguments[: page_match.start()].rstrip()
    else:
        if _SHOWIMG_PAGE_TOKEN_RE.search(raw_arguments) is not None:
            raise _ShowImageInputError("--page N 必须位于命令末尾")
        file_pattern = raw_arguments

    if not file_pattern or "\0" in file_pattern:
        raise _ShowImageInputError("图片路径或通配符不能为空")
    if len(file_pattern) > _MAX_SHOWIMG_PATTERN_CHARS:
        raise _ShowImageInputError(f"图片匹配表达式过长（最大 {_MAX_SHOWIMG_PATTERN_CHARS} 字符）")
    return _ShowImageRequest(pattern=file_pattern, page=page)


def _resolve_showimg_listing(pattern: str, cwd: str | None) -> tuple[str, str]:
    """把远端路径拆为明确目录和最后一级文件名通配符。"""

    directory, filename_pattern = posixpath.split(pattern)
    if not filename_pattern:
        filename_pattern = "*"
    if any(marker in directory for marker in ("*", "?", "[")):
        raise _ShowImageInputError("目录部分需要明确路径，通配符请放在最后一级文件名中")

    if not directory:
        return cwd or ".", filename_pattern
    if posixpath.isabs(directory):
        remote_dir = posixpath.normpath(directory)
    elif cwd:
        remote_dir = posixpath.normpath(posixpath.join(cwd, directory))
    else:
        remote_dir = posixpath.normpath(directory)
    return remote_dir or "/", filename_pattern


def _parse_export_environment(
    text: str,
    env_vars: dict[str, str],
) -> tuple[dict[str, str] | None, str | None]:
    """解析一条 export 赋值，并执行会话环境变量数量与长度预算。"""

    export_match = _EXPORT_RE.fullmatch(text)
    if export_match is None:
        return None, None

    var_name = export_match.group(1)
    raw_value = export_match.group(2).strip()
    if not raw_value:
        var_value = ""
    else:
        try:
            parsed_value = shlex.split(raw_value, comments=False, posix=True)
        except ValueError:
            return None, "export 值的引号不完整"
        if len(parsed_value) != 1:
            return None, "export 值必须是一个 Shell 值"
        var_value = parsed_value[0]

    candidate_env = {**env_vars, var_name: var_value}
    if len(var_value) > _MAX_ENV_VALUE_CHARS:
        return None, f"环境变量值过长（最大 {_MAX_ENV_VALUE_CHARS} 字符）"
    if len(candidate_env) > _MAX_ENV_VARS:
        return None, f"环境变量过多（最多 {_MAX_ENV_VARS} 个）"
    if sum(len(key) + len(value) for key, value in candidate_env.items()) > _MAX_ENV_TOTAL_CHARS:
        return None, f"环境变量总长度过大（最大 {_MAX_ENV_TOTAL_CHARS} 字符）"
    return candidate_env, None


@dataclass(frozen=True, slots=True)
class _CommandJob:
    """一个后台命令及其所属的不可变会话代次。"""

    key: _SessionJobKey
    server_name: str
    job_id: str
    task: asyncio.Task[None]


# 按唯一 ID 保存全部存活任务，避免新会话代次覆盖索引后，关闭流程漏掉旧任务。
# 第二个索引只指向每个用户/群会话的当前代次；任务结束时同时做对象与 ID 比较。
_COMMAND_JOBS: dict[str, _CommandJob] = {}
_CURRENT_JOB_BY_KEY: dict[_SessionJobKey, str] = {}
_SHUTTING_DOWN = False


class _JobGenerationChanged(Exception):
    """终止旧任务事务，同时不改动当前会话代次。"""


def _session_job_key(context: Context) -> _SessionJobKey:
    return (context.current_user_id, context.current_group_id)


def _contains_sensitive_history_data(command: str) -> bool:
    """识别常见的明文凭据语法，避免便利历史变成秘密存储。"""

    return any(
        pattern.search(command) is not None
        for pattern in (
            _SENSITIVE_HISTORY_ASSIGNMENT_RE,
            _SENSITIVE_HISTORY_OPTION_RE,
            _SENSITIVE_HISTORY_HEADER_RE,
            _URL_CREDENTIAL_RE,
        )
    )


def _session_history(session: Session) -> list[str]:
    """返回安全历史副本，并顺手清除旧版本遗留的敏感或损坏条目。"""

    history = session.get(SessionKeys.HISTORY, [])
    if not isinstance(history, list):
        session.set(SessionKeys.HISTORY, [])
        return []
    safe_history = [
        item
        for item in history
        if isinstance(item, str) and not _contains_sensitive_history_data(item)
    ]
    if safe_history != history:
        session.set(SessionKeys.HISTORY, safe_history)
    return safe_history


def _new_job_id() -> str:
    while True:
        job_id = uuid.uuid4().hex
        if job_id not in _COMMAND_JOBS:
            return job_id


def _register_job(job: _CommandJob) -> None:
    _COMMAND_JOBS[job.job_id] = job
    _CURRENT_JOB_BY_KEY[job.key] = job.job_id


def _remove_job_if_current(job: _CommandJob) -> None:
    if _COMMAND_JOBS.get(job.job_id) is job:
        _COMMAND_JOBS.pop(job.job_id, None)
    if _CURRENT_JOB_BY_KEY.get(job.key) == job.job_id:
        _CURRENT_JOB_BY_KEY.pop(job.key, None)


def _find_job(
    key: _SessionJobKey,
    server_name: object,
    job_id: object,
) -> _CommandJob | None:
    if not isinstance(job_id, str) or not job_id:
        return None
    job = _COMMAND_JOBS.get(job_id)
    if job is None or job.key != key or job.server_name != server_name:
        return None
    return job


def _session_owns_job(
    session: Session,
    *,
    server_name: str,
    job_id: str,
) -> bool:
    return (
        getattr(session, "plugin_name", None) == "qingssh"
        and session.get(SessionKeys.SERVER_NAME) == server_name
        and session.get(SessionKeys.STATE) == "executing"
        and session.get(SessionKeys.CURRENT_TASK) == job_id
    )


async def _session_job_is_current(
    update_session: Callable[[Callable[[Session], Any]], Awaitable[Any]],
    *,
    server_name: str,
    job_id: str,
) -> bool:
    def check(current: Session) -> bool:
        if not _session_owns_job(current, server_name=server_name, job_id=job_id):
            raise _JobGenerationChanged
        return True

    try:
        return await update_session(check) is True
    except _JobGenerationChanged:
        return False


async def _commit_job_result(
    update_session: Callable[[Callable[[Session], Any]], Awaitable[Any]],
    *,
    server_name: str,
    job_id: str,
    cwd: str | None,
) -> bool:
    def commit(current: Session) -> bool:
        if not _session_owns_job(current, server_name=server_name, job_id=job_id):
            raise _JobGenerationChanged
        if cwd is not None:
            current.set(SessionKeys.CWD, cwd)
        current.set(SessionKeys.STATE, "connected")
        current.set(SessionKeys.CURRENT_TASK, None)
        return True

    try:
        return await update_session(commit) is True
    except _JobGenerationChanged:
        return False


async def _commit_job_result_resilient(
    update_session: Callable[[Callable[[Session], Any]], Awaitable[Any]],
    *,
    server_name: str,
    job_id: str,
    cwd: str | None,
) -> None:
    """即使任务被重复取消，也先完成会话代次的比较并交换清理。"""

    cleanup = asyncio.create_task(
        _commit_job_result(
            update_session,
            server_name=server_name,
            job_id=job_id,
            cwd=cwd,
        ),
        name=f"qingssh-session-cleanup-{job_id[:12]}",
    )
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            if cleanup.cancelled():
                break
            if cancellation is None:
                cancellation = exc
            continue
        except BaseException:
            break

    cleanup.result()
    if cancellation is not None:
        raise cancellation


async def close_session(context: Context, session: Session) -> None:
    server_name = session.get(SessionKeys.SERVER_NAME)
    job_id = session.get(SessionKeys.CURRENT_TASK)
    job = _find_job(_session_job_key(context), server_name, job_id)
    if job is not None:
        # close_session 通常持有父会话事务：先把准确代次标记为已关闭，再只发出取消；
        # 子任务会在键锁释放后完成 CAS，父事务即使回滚也不会永久停在 executing。
        if _session_owns_job(session, server_name=job.server_name, job_id=job.job_id):
            session.set(SessionKeys.STATE, "connected")
            session.set(SessionKeys.CURRENT_TASK, None)
        if not job.task.done():
            job.task.cancel()

    # 任务取消必须先行；损坏会话没有服务器名时无需反向创建管理器。
    if isinstance(server_name, str) and server_name:
        manager = await get_manager(context)
        if job is not None:
            try:
                await manager.stop_command(
                    str(context.current_user_id),
                    str(context.current_group_id),
                    server_name,
                )
            except Exception as exc:
                logger.warning(
                    "SSH close remote stop failed error_type=%s",
                    audit_error_type(exc),
                )
        manager.disconnect(str(context.current_user_id), str(context.current_group_id), server_name)


async def shutdown_tasks() -> None:
    global _SHUTTING_DOWN

    _SHUTTING_DOWN = True
    try:
        # 关闭期间拒绝新命令；循环收集，覆盖刚好在标志生效前注册的任务。
        while _COMMAND_JOBS:
            jobs = list(_COMMAND_JOBS.values())
            for job in jobs:
                if not job.task.done():
                    job.task.cancel()
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
            for job in jobs:
                _remove_job_if_current(job)
    finally:
        _SHUTTING_DOWN = False


async def ensure_session_connected(
    context: Context, session: Session, manager: SSHManager
) -> tuple[bool, str]:
    """确认会话保存了合法服务器名且底层连接仍然有效。"""
    server_name = session.get(SessionKeys.SERVER_NAME)
    if not isinstance(server_name, str) or not server_name:
        await context.end_session()
        return False, "❌ SSH 会话状态无效，请使用 /ssh 重新连接"
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)

    if not manager.is_connected(user_id, group_id, server_name):
        await context.end_session()
        return False, f"❌ 与服务器 {server_name} 的连接已断开\n\n使用 /ssh 重新连接"

    return True, ""


async def handle_session(
    text: str,
    event: OneBotEvent,
    context: Context,
    session: Session,
) -> MessageSegments:
    """按会话状态分发添加流程或已连接命令。"""
    state = session.get(SessionKeys.STATE, "connected")

    if state == "adding":
        manager = await get_manager(context)
        return await _handle_adding_session(text, context, session, manager)
    if state in {"connected", "executing"}:
        manager = await get_manager(context)
        return await _handle_connected_session(text, context, session, manager)
    await context.end_session()
    return segments("❌ SSH 会话状态无效，请使用 /ssh 重新连接")


async def _finish_server_add(
    context: Context,
    manager: SSHManager,
    config: _ServerDraft,
    auth_type: str,
    *,
    password: str | None = None,
    key_path: str | None = None,
) -> MessageSegments:
    """校验完整草稿，持久化服务器并生成统一完成消息。"""

    name = config.get("name")
    host = config.get("host")
    port = config.get("port")
    username = config.get("username")
    if (
        not isinstance(name, str)
        or not isinstance(host, str)
        or type(port) is not int
        or not isinstance(username, str)
    ):
        await context.end_session()
        return segments("❌ 添加会话状态无效，请重新执行 /ssh添加")

    added = await manager.add_server(
        name,
        host,
        port,
        username,
        auth_type,
        password=password,
        key_path=key_path,
    )
    if not added:
        return segments("❌ 服务器保存失败，请稍后重试")

    await context.end_session()
    return segments(format_server_added(name, host, port, username, auth_type))


async def _handle_adding_session(
    text: str, context: Context, session: Session, manager: SSHManager
) -> MessageSegments:
    """推进引导式服务器添加流程。"""

    raw_text = text
    text = text.strip()

    if text.casefold() in CANCEL_KEYWORDS:
        await context.end_session()
        return segments("❌ 已取消添加服务器")

    step = session.get(SessionKeys.STEP)
    raw_config = session.get(SessionKeys.SERVER_CONFIG, {})
    required_fields = _REQUIRED_DRAFT_FIELDS.get(step) if isinstance(step, str) else None
    if (
        required_fields is None
        or not isinstance(raw_config, dict)
        or any(field not in raw_config for field in required_fields)
    ):
        await context.end_session()
        return segments("❌ 添加会话状态无效，请重新执行 /ssh添加")
    config = cast(_ServerDraft, dict(raw_config))

    if step == "name":
        is_valid, error_msg = validate_server_name(text)
        if not is_valid:
            return segments(f"❌ {error_msg}")
        if manager.get_server(text) is not None:
            return segments(f"❌ 服务器 '{text}' 已存在，请使用其他名称")

        config["name"] = text
        session.set(SessionKeys.SERVER_CONFIG, config)
        session.set(SessionKeys.STEP, "host")

        return segments(f"✅ 名称: {text}\n\n请输入主机地址（IP或域名）:")

    if step == "host":
        is_valid, error_msg = validate_hostname(text)
        if not is_valid:
            return segments(f"❌ {error_msg}")
        config["host"] = text
        session.set(SessionKeys.SERVER_CONFIG, config)
        session.set(SessionKeys.STEP, "port")

        return segments(f"✅ 主机: {text}\n\n请输入端口号（默认22，直接回车跳过）:")

    if step == "port":
        if text:
            is_valid, port, error_msg = validate_port(text)
            if not is_valid:
                return segments(f"❌ {error_msg}")
            config["port"] = port
        else:
            config["port"] = 22

        session.set(SessionKeys.SERVER_CONFIG, config)
        session.set(SessionKeys.STEP, "username")

        return segments(f"✅ 端口: {config['port']}\n\n请输入用户名（默认root，直接回车跳过）:")

    if step == "username":
        username = text or "root"
        is_valid, error_msg = validate_username(username)
        if not is_valid:
            return segments(f"❌ {error_msg}")
        config["username"] = username
        session.set(SessionKeys.SERVER_CONFIG, config)
        session.set(SessionKeys.STEP, "auth_type")

        return segments(
            f"✅ 用户名: {config['username']}\n\n"
            "请选择认证方式:\n"
            "1. 密码认证 (输入 1 或 password)\n"
            "2. 密钥认证 (输入 2 或 key)\n"
            "3. SSH Agent (输入 3 或 agent)"
        )

    if step == "auth_type":
        choice = text.casefold()
        if choice in {"1", "password", "密码"}:
            if context.current_group_id is not None:
                return segments(
                    "❌ 密码只能在管理员私聊中输入；请选择密钥/Agent，或私聊后重新执行 /ssh添加"
                )
            config["auth_type"] = "password"
            session.set(SessionKeys.SERVER_CONFIG, config)
            session.set(SessionKeys.STEP, "password")
            return segments("请输入密码:")
        if choice in {"2", "key", "密钥"}:
            config["auth_type"] = "key"
            session.set(SessionKeys.SERVER_CONFIG, config)
            session.set(SessionKeys.STEP, "key_path")
            return segments("请输入密钥文件路径（如 ~/.ssh/id_rsa）:")
        if choice in {"3", "agent"}:
            config["auth_type"] = "agent"
            session.set(SessionKeys.SERVER_CONFIG, config)
            return await _finish_server_add(context, manager, config, "agent")
        return segments("❌ 请输入 1、2 或 3 选择认证方式")

    if step == "password":
        if context.current_group_id is not None:
            await context.end_session()
            return segments("❌ 已终止：密码认证配置只能在管理员私聊中完成")

        if raw_text == "":
            return segments("❌ 密码不能为空")
        return await _finish_server_add(
            context,
            manager,
            config,
            "password",
            password=raw_text,
        )

    if step == "key_path":
        key_path = Path(text).expanduser()
        if not key_path.is_file():
            return segments(
                f"⚠️ 密钥文件不存在: {key_path.as_posix()}\n\n请重新输入密钥路径，或确认文件位置:"
            )
        config["key_path"] = key_path.as_posix()
        session.set(SessionKeys.SERVER_CONFIG, config)
        return await _finish_server_add(
            context,
            manager,
            config,
            "key",
            key_path=config["key_path"],
        )

    await context.end_session()
    return segments("❌ 添加会话状态无效，请重新执行 /ssh添加")


async def _handle_connected_session(
    text: str, context: Context, session: Session, manager: SSHManager
) -> MessageSegments:
    """处理会话内帮助、状态、历史、图片和远端命令。"""

    if _SHUTTING_DOWN:
        return segments("⚠️ QingSSH 正在重载，请稍后重试")

    server_name = session.get(SessionKeys.SERVER_NAME)
    if not isinstance(server_name, str) or not server_name:
        await context.end_session()
        return segments("❌ SSH 会话状态无效，请使用 /ssh 重新连接")
    raw_command_count = session.get(SessionKeys.COMMAND_COUNT, 0)
    command_count = (
        raw_command_count
        if isinstance(raw_command_count, int) and not isinstance(raw_command_count, bool)
        else 0
    )

    is_valid, error_msg = await ensure_session_connected(context, session, manager)
    if not is_valid:
        return segments(error_msg)

    text = text.strip()

    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)

    if session.get(SessionKeys.STATE) == "executing":
        if text.casefold() in STOP_KEYWORDS:
            stop_result = await manager.stop_command(user_id, group_id, server_name)
            if not stop_result.found:
                return segments("⚠️ 未找到运行中的命令")
            if stop_result.remote_confirmed:
                return segments("🛑 远端命令已确认停止，命令通道已清理")
            if stop_result.local_cleaned:
                return segments("⚠️ 已关闭命令通道，但远端进程状态未知，请登录服务器确认")
            return segments("⚠️ 命令通道清理失败，且远端进程状态未知，请登录服务器确认")
        return segments("⏳ 有命令正在运行中...\n发送「停止」可强制结束，或等待命令完成。")

    # Dispatcher 在把输入交给活跃会话前会移除命令前缀，因此用户发送的
    # ``/help`` 到这里时已经是 ``help``。
    if text.casefold() in {"help", "/help", "ssh帮助", "插件帮助", "帮助"}:
        return segments(
            "🖥️ SSH 会话帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 直接输入命令执行\n"
            "💡 普通命令会被记住；密钥、token 等敏感命令不会进入历史\n"
            "💡 输入「状态」查看当前目录\n"
            "💡 输入「历史」查看命令历史\n"
            "💡 输入「!!」重复上一条命令\n"
            "💡 showimg <路径或通配符> [--page N]：顺序发送图片\n"
            "💡 通配符支持 *、?、[]；每页 5 张，用 --page 查看后续\n"
            "💡 输入「退出」/「取消」结束会话\n"
            "💡 输入「停止」中断运行中的命令\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

    if text.casefold() in {"状态", "status"}:
        server = manager.get_server(server_name)
        raw_cwd = session.get(SessionKeys.CWD, "~")
        status_cwd = raw_cwd if isinstance(raw_cwd, str) else "~"
        raw_env_vars = session.get(SessionKeys.ENV_VARS, {})
        env_vars = (
            {
                key: value
                for key, value in raw_env_vars.items()
                if isinstance(key, str)
                and _ENV_NAME_RE.fullmatch(key) is not None
                and isinstance(value, str)
            }
            if isinstance(raw_env_vars, dict)
            else {}
        )
        info = f"已连接: {server_name}"
        if server is not None:
            info += f"\n主机: {server.get('host', '?')}"
        info += f"\n当前目录: {status_cwd}"
        if env_vars:
            info += f"\n环境变量: {len(env_vars)} 个"
        return segments(f"🖥️ {info}\n已执行命令: {command_count}")

    if text.casefold() in {"历史", "history"}:
        history = _session_history(session)
        if not history:
            return segments("📜 命令历史为空")
        lines = ["📜 命令历史 (最近 20 条)", "━━━━━━━━━━━━━━━━━━"]
        for i, cmd in enumerate(history[-20:], 1):
            lines.append(f"{i:2d}. {cmd[:50]}{'...' if len(cmd) > 50 else ''}")
        return segments("\n".join(lines))

    if text == "!!":
        history = _session_history(session)
        if history:
            text = history[-1]
        else:
            return segments("❌ 没有历史命令可重复")

    if _is_showimg_command(text):
        return await _handle_showimg_command(text, context, session, manager)

    is_valid, error_msg = validate_command(text)
    if not is_valid:
        return segments(f"❌ {error_msg}")

    try:
        output_policy = SSHOutputPolicy.from_context(context)
    except ValueError as exc:
        return segments(f"❌ QingSSH 输出配置无效: {exc}")

    raw_cwd = session.get(SessionKeys.CWD)
    cwd = raw_cwd if isinstance(raw_cwd, str) else None
    raw_env_vars = session.get(SessionKeys.ENV_VARS, {})
    env_vars = (
        {
            key: value
            for key, value in raw_env_vars.items()
            if isinstance(key, str)
            and _ENV_NAME_RE.fullmatch(key) is not None
            and isinstance(value, str)
        }
        if isinstance(raw_env_vars, dict)
        else {}
    )

    candidate_env, env_error = _parse_export_environment(text, env_vars)
    if env_error is not None:
        return segments(f"❌ {env_error}")
    if candidate_env is not None:
        env_vars = candidate_env
        session.set(SessionKeys.ENV_VARS, candidate_env)

    is_cd = is_cd_command(text)

    history = _session_history(session)
    if not _contains_sensitive_history_data(text):
        history.append(text)
        if len(history) > MAX_HISTORY_LENGTH:
            history = history[-MAX_HISTORY_LENGTH:]
        session.set(SessionKeys.HISTORY, history)

    # build_command 会统一应用持久化目录和环境变量，并让 cd 追加 pwd。
    actual_command = build_command(text, cwd, env_vars)

    # 父会话事务先记录代次，子任务提交后才能越过 CAS 屏障连接 SSH。
    session.set(SessionKeys.STATE, "executing")
    session.set(SessionKeys.COMMAND_COUNT, command_count + 1)

    # 只记录不可逆的审计摘要；原始命令仍完整交给可信管理员和 SSH 后端。
    command_audit = summarize_sensitive(actual_command)
    job_id = _new_job_id()
    request_id = audit_request_id(context)
    logger.info(
        "SSH audit operation=command status=started request_id=%s job_id=%s "
        "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
        request_id,
        job_id,
        command_audit.kind,
        command_audit.length,
        command_audit.byte_length,
        command_audit.fingerprint,
    )

    # 这里仍处于父会话事务中；子任务的第一次
    # update_session 会等待该事务提交，然后核验这个 job_id。
    target_user_id = context.current_user_id
    target_group_id = context.current_group_id
    job_key = _session_job_key(context)
    session.set(SessionKeys.CURRENT_TASK, job_id)
    background = _run_background_command(
        context.update_session,
        context.send_action,
        manager,
        server_name,
        actual_command,
        user_id,
        group_id,
        target_user_id,
        target_group_id,
        output_policy,
        is_cd=is_cd,
        job_key=job_key,
        job_id=job_id,
        request_id=request_id,
    )
    try:
        task = asyncio.create_task(background, name=f"qingssh-command-{job_id[:12]}")
    except BaseException:
        background.close()
        raise
    _register_job(
        _CommandJob(
            key=job_key,
            server_name=server_name,
            job_id=job_id,
            task=task,
        )
    )

    return segments(f"🚀 命令已启动: {text}\n发送「停止」可中断...")


async def _run_background_command(
    update_session: Callable[[Callable[[Session], Any]], Awaitable[Any]],
    send_action: Callable[[Any], Awaitable[Any]],
    manager: SSHManager,
    server_name: str,
    command: str,
    conn_user_id: str,
    conn_group_id: str,
    user_id: Any,
    group_id: Any,
    output_policy: SSHOutputPolicy,
    *,
    job_key: _SessionJobKey,
    job_id: str,
    is_cd: bool = False,
    request_id: str | None = None,
) -> None:
    """核验会话代次后执行命令、投影输出，并用 CAS 提交最终状态。"""

    cd_output_tail = ""
    relay: SSHOutputRelay | None = None
    relay_finished = False
    resolved_cwd: str | None = None
    command_audit = summarize_sensitive(command)
    audit_job_id = audit_id(job_id)
    request_id = audit_id(request_id)

    async def send_text(content: str) -> None:
        action = build_action(segments(content), user_id, group_id)
        if action:
            action[ACTION_BYPASS_SINK_KEY] = True
            await send_action(action)

    async def output_callback(text: str) -> None:
        nonlocal cd_output_tail
        if is_cd:
            cd_output_tail = (cd_output_tail + text)[-8192:]
            text = strip_cwd_markers(text)
        if relay is not None:
            await relay.feed(text)

    async def abort_relay() -> None:
        if relay is None:
            return
        cleanup = asyncio.create_task(relay.abort(), name="qingssh-output-cleanup")
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()

    try:
        registered = _find_job(job_key, server_name, job_id)
        current_task = asyncio.current_task()
        if (
            registered is None
            or registered.task is not current_task
            or _CURRENT_JOB_BY_KEY.get(job_key) != job_id
        ):
            return
        # 与父会话事务同步；父事务回滚、删除或替换后，不得触碰 SSH。
        if not await _session_job_is_current(
            update_session,
            server_name=server_name,
            job_id=job_id,
        ):
            return

        relay = SSHOutputRelay(
            output_dir=Path(manager.data_dir) / "command_outputs",
            policy=output_policy,
            send_text=send_text,
        )
        exit_code = await manager.execute_command_stream(
            conn_user_id,
            conn_group_id,
            server_name,
            command,
            output_callback,
            timeout=output_policy.command_timeout_seconds,
        )

        if exit_code == EXIT_CODE_INTERRUPTED:
            result_msg = "⏹️ 命令已中断"
        elif exit_code == EXIT_CODE_TIMEOUT:
            result_msg = f"⏱️ 命令执行超时 ({output_policy.command_timeout_seconds:g}s)"
        elif exit_code != 0:
            result_msg = f"⚠️ 命令失败 [退出码: {exit_code}]"
        else:
            result_msg = "✅ 命令执行完毕"
            if is_cd:
                new_cwd = extract_cwd_from_output(cd_output_tail)
                if new_cwd:
                    resolved_cwd = new_cwd

        summary: SSHOutputSummary = await relay.finish(result_msg)
        relay_finished = True
        logger.info(
            "SSH audit operation=command status=completed request_id=%s job_id=%s "
            "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
            "exit_code=%d output_chars=%d output_bytes=%d",
            request_id,
            audit_job_id,
            command_audit.kind,
            command_audit.length,
            command_audit.byte_length,
            command_audit.fingerprint,
            exit_code,
            summary.total_chars,
            summary.total_bytes,
        )
        if summary.delivery_errors:
            logger.warning(
                "SSH QQ projection status=failed job_id=%s attempts=%s failures=%s",
                audit_job_id,
                summary.actions_attempted,
                summary.delivery_errors,
            )
        if summary.qq_truncated:
            archive_created = summary.archive_path is not None
            logger.info(
                "SSH output projection status=truncated job_id=%s chars=%s bytes=%s "
                "archive_created=%s",
                audit_job_id,
                summary.total_chars,
                summary.total_bytes,
                archive_created,
            )
    except asyncio.CancelledError:
        logger.info(
            "SSH audit operation=command status=cancelled request_id=%s job_id=%s "
            "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
            request_id,
            audit_job_id,
            command_audit.kind,
            command_audit.length,
            command_audit.byte_length,
            command_audit.fingerprint,
        )
        await abort_relay()
        raise
    except Exception as exc:
        logger.error(
            "SSH audit operation=command status=failed request_id=%s job_id=%s "
            "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
            "error_type=%s",
            request_id,
            audit_job_id,
            command_audit.kind,
            command_audit.length,
            command_audit.byte_length,
            command_audit.fingerprint,
            audit_error_type(exc),
        )
        if relay is not None:
            try:
                await relay.finish("❌ 命令执行出错，请查看日志")
                relay_finished = True
            except asyncio.CancelledError:
                await abort_relay()
                raise
            except Exception as finalize_exc:
                logger.error(
                    "SSH error projection finalization failed job_id=%s error_type=%s",
                    audit_job_id,
                    audit_error_type(finalize_exc),
                )
    finally:
        if relay is not None and not relay_finished:
            try:
                await abort_relay()
            except Exception as cleanup_exc:
                logger.error(
                    "SSH output relay cleanup failed job_id=%s error_type=%s",
                    audit_job_id,
                    audit_error_type(cleanup_exc),
                )
        try:
            await _commit_job_result_resilient(
                update_session,
                server_name=server_name,
                job_id=job_id,
                cwd=resolved_cwd,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("SSH session CAS cleanup failed error_type=%s", audit_error_type(exc))
        finally:
            registered = _COMMAND_JOBS.get(job_id)
            current_task = asyncio.current_task()
            if registered is not None and registered.task is current_task:
                _remove_job_if_current(registered)


async def _handle_showimg_command(
    text: str, context: Context, session: Session, manager: SSHManager
) -> MessageSegments:
    """按远端路径匹配图片，并按每页五张下载发送。"""

    try:
        request = _parse_showimg_request(text)
    except _ShowImageInputError as exc:
        return segments(f"❌ {exc}\n{_SHOWIMG_USAGE}")

    server_name = session.get(SessionKeys.SERVER_NAME)
    if not isinstance(server_name, str) or not server_name:
        await context.end_session()
        return segments("❌ SSH 会话状态无效，请使用 /ssh 重新连接")
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)

    is_valid, error_msg = await ensure_session_connected(context, session, manager)
    if not is_valid:
        return segments(error_msg)

    raw_cwd = session.get(SessionKeys.CWD)
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd.startswith("/") else None

    if not cwd:
        success, pwd_output = await manager.execute_command(user_id, group_id, server_name, "pwd")
        resolved_pwd = pwd_output.strip()
        if success and resolved_pwd.startswith("/"):
            cwd = resolved_pwd

    try:
        remote_dir, filename_pattern = _resolve_showimg_listing(request.pattern, cwd)
    except _ShowImageInputError as exc:
        return segments(f"❌ {exc}\n{_SHOWIMG_USAGE}")

    success, files = await manager.list_files(
        user_id, group_id, server_name, remote_dir, filename_pattern
    )

    if not success or not files:
        return segments(f"❌ 未找到匹配的文件: {request.pattern}\n搜索目录: {remote_dir}")

    image_files = sorted(
        filename for filename in files if Path(filename).suffix.lower() in _IMAGE_EXTENSIONS
    )

    if not image_files:
        return segments(f"❌ 未找到图片文件\n匹配的文件: {', '.join(files)}")

    matched_image_count = len(image_files)
    total_pages = (matched_image_count + _MAX_SHOWIMG_FILES - 1) // _MAX_SHOWIMG_FILES
    if request.page > total_pages:
        return segments(
            f"❌ 页码超出范围：共 {total_pages} 页、{matched_image_count} 张图片\n{_SHOWIMG_USAGE}"
        )
    page_start = (request.page - 1) * _MAX_SHOWIMG_FILES
    page_files = image_files[page_start : page_start + _MAX_SHOWIMG_FILES]

    configured_data_dir = getattr(context, "data_dir", None)
    data_dir = (
        Path(configured_data_dir)
        if isinstance(configured_data_dir, (str, Path))
        else Path(context.plugin_dir) / "data"
    )
    images_dir = data_dir / "images"
    await asyncio.to_thread(BoundedFileCache(images_dir, _IMAGE_CACHE_LIMITS).prune)

    downloaded_files: list[tuple[int, str, Path]] = []
    local_paths: list[Path] = []
    errors: list[str] = []
    try:
        for global_index, filename in enumerate(page_files, page_start + 1):
            remote_path = resolve_remote_path(filename, remote_dir)
            local_path = images_dir / f"{uuid.uuid4().hex}{Path(filename).suffix}"
            local_paths.append(local_path)
            success, message = await manager.download_file(
                user_id,
                group_id,
                server_name,
                remote_path,
                str(local_path),
                max_bytes=_MAX_SHOWIMG_BYTES,
            )
            if success:
                downloaded_files.append((global_index, filename, local_path))
            else:
                errors.append(f"{filename}: {message}")

        message_parts: list[str] = []
        if downloaded_files:
            sent_count = len(downloaded_files)
            for global_index, filename, local_path in downloaded_files:
                action = build_action(
                    [
                        text_segment(f"📷 {global_index}/{matched_image_count}\n{filename}\n"),
                        image(str(local_path)),
                    ],
                    context.current_user_id,
                    context.current_group_id,
                )
                if action:
                    action[ACTION_BYPASS_SINK_KEY] = True
                    await context.send_action(action)
            message_parts.append(
                f"✅ 第 {request.page}/{total_pages} 页已按文件名顺序发送 {sent_count} 张图片"
            )

        navigation: list[str] = []
        if request.page > 1:
            navigation.append(f"⬅️ 上一页：showimg {request.pattern} --page {request.page - 1}")
        if request.page < total_pages:
            navigation.append(f"➡️ 下一页：showimg {request.pattern} --page {request.page + 1}")
        if navigation:
            message_parts.append(f"📄 共 {matched_image_count} 张图片\n" + "\n".join(navigation))

        if errors:
            error_msg = f"❌ 下载失败 ({len(errors)} 个):\n" + "\n".join(
                f"  • {error}" for error in errors[:5]
            )
            if len(errors) > 5:
                error_msg += f"\n  ... 及其他 {len(errors) - 5} 个"
            message_parts.append(error_msg)

        return segments("\n\n".join(message_parts))
    finally:
        # OneBot 已确认接收动作后即可删除临时文件；异常和取消路径同样必须清理。
        for local_path in local_paths:
            with suppress(OSError):
                local_path.unlink(missing_ok=True)
