"""
SSH 会话处理器

处理多轮对话中的会话消息（添加服务器流程、命令执行）

退出命令处理：
框架支持的退出命令：{"退出", "取消", "exit", "quit", "q"}
插件会拦截这些命令，先断开 SSH 连接再结束会话，避免连接泄露。
"""

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from core.constants import EXIT_COMMANDS_SET
from core.plugin_base import segments
from core.sensitive_audit import summarize_sensitive

from .audit import audit_error_type, audit_id, audit_request_id
from .config import (
    CANCEL_KEYWORDS,
    EXIT_CODE_INTERRUPTED,
    EXIT_CODE_TIMEOUT,
    MAX_HISTORY_LENGTH,
    STOP_KEYWORDS,
    SessionKeys,
    SSHDefaults,
)
from .message_formatter import format_server_added
from .output_relay import SSHOutputPolicy, SSHOutputRelay, SSHOutputSummary
from .path_resolver import (
    build_command,
    extract_cwd_from_output,
    is_cd_command,
    resolve_remote_path,
)
from .ssh_manager import SSHManager, get_manager
from .types import Context, MessageSegments, OneBotEvent, Session
from .validators import validate_hostname, validate_port, validate_server_name

logger = logging.getLogger(__name__)

_SESSION_TASKS: dict[str, asyncio.Task[Any]] = {}
MAX_SHOWIMG_FILES = 5
MAX_SHOWIMG_BYTES = 10 * 1024 * 1024


def _session_task_key(context: Context, session: Session) -> str:
    server_name = session.get(SessionKeys.SERVER_NAME, "")
    return f"{context.current_user_id}:{context.current_group_id}:{server_name}"


def _get_session_task(context: Context, session: Session) -> asyncio.Task[Any] | None:
    return _SESSION_TASKS.get(_session_task_key(context, session))


def _set_session_task(context: Context, session: Session, task: asyncio.Task[Any] | None) -> None:
    key = _session_task_key(context, session)
    if task is None:
        _SESSION_TASKS.pop(key, None)
        return
    _SESSION_TASKS[key] = task


async def close_session(context: Context, session: Session) -> None:
    task = _get_session_task(context, session)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _set_session_task(context, session, None)
    manager = await get_manager(context)
    server_name = session.get(SessionKeys.SERVER_NAME)
    if server_name:
        manager.disconnect(str(context.current_user_id), str(context.current_group_id), server_name)


async def shutdown_tasks() -> None:
    tasks = list(_SESSION_TASKS.values())
    _SESSION_TASKS.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def ensure_session_connected(
    context: Context, session: Session, manager: SSHManager
) -> tuple[bool, str]:
    """
    确保会话的 SSH 连接仍然有效

    Args:
        context: 插件上下文
        session: 会话对象
        manager: SSH 管理器

    Returns:
        (is_valid, error_message)
    """
    server_name = session.get(SessionKeys.SERVER_NAME)
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
    """
    处理会话消息

    当用户有活跃会话时，Dispatcher 会调用这个函数处理后续消息。

    参数:
        text: 用户发送的原始文本
        event: OneBot 事件
        context: 插件上下文
        session: 当前会话对象
    """
    manager = await get_manager(context)
    state = session.get(SessionKeys.STATE, "connected")

    # 处理添加服务器的多步骤
    if state == "adding":
        return await _handle_adding_session(text, context, session, manager)

    # 处理已连接状态的命令执行
    return await _handle_connected_session(text, context, session, manager)


async def _handle_adding_session(
    text: str, context: Context, session: Session, manager: SSHManager
) -> MessageSegments:
    """处理添加服务器的多步骤会话"""

    text = text.strip()

    # 检查是否取消
    if text.lower() in CANCEL_KEYWORDS or text in CANCEL_KEYWORDS:
        await context.end_session()
        return segments("❌ 已取消添加服务器")

    step = session.get("step")
    config = session.get("server_config", {})

    if step == "name":
        is_valid, error_msg = validate_server_name(text)
        if not is_valid:
            return segments(f"❌ {error_msg}")
        if manager.get_server(text):
            return segments(f"❌ 服务器 '{text}' 已存在，请使用其他名称")

        config["name"] = text
        session.set("server_config", config)
        session.set("step", "host")

        return segments(f"✅ 名称: {text}\n\n请输入主机地址（IP或域名）:")

    elif step == "host":
        is_valid, error_msg = validate_hostname(text)
        if not is_valid:
            return segments(f"❌ {error_msg}")
        config["host"] = text
        session.set("server_config", config)
        session.set("step", "port")

        return segments(f"✅ 主机: {text}\n\n请输入端口号（默认22，直接回车跳过）:")

    elif step == "port":
        if text:
            is_valid, port, error_msg = validate_port(text)
            if not is_valid:
                return segments(f"❌ {error_msg}")
            config["port"] = port
        else:
            config["port"] = 22

        session.set("server_config", config)
        session.set("step", "username")

        return segments(f"✅ 端口: {config['port']}\n\n请输入用户名（默认root，直接回车跳过）:")

    elif step == "username":
        config["username"] = text if text else "root"
        session.set("server_config", config)
        session.set("step", "auth_type")

        return segments(
            f"✅ 用户名: {config['username']}\n\n"
            "请选择认证方式:\n"
            "1. 密码认证 (输入 1 或 password)\n"
            "2. 密钥认证 (输入 2 或 key)\n"
            "3. SSH Agent (输入 3 或 agent)"
        )

    elif step == "auth_type":
        if text in {"1", "password", "密码"}:
            if context.current_group_id is not None:
                return segments("❌ 密码只能由管理员在私聊中输入；请私聊机器人重新执行 /ssh add")
            config["auth_type"] = "password"
            session.set("server_config", config)
            session.set("step", "password")
            return segments("请输入密码:")
        elif text in {"2", "key", "密钥"}:
            config["auth_type"] = "key"
            session.set("server_config", config)
            session.set("step", "key_path")
            return segments("请输入密钥文件路径（如 ~/.ssh/id_rsa）:")
        elif text in {"3", "agent"}:
            config["auth_type"] = "agent"
            session.set("server_config", config)

            # 使用 Agent，直接完成添加
            await manager.add_server(
                config["name"],
                config["host"],
                config.get("port", SSHDefaults.PORT),
                config.get("username", SSHDefaults.USERNAME),
                "agent",
            )

            await context.end_session()

            return segments(
                format_server_added(
                    config["name"],
                    config["host"],
                    config.get("port", SSHDefaults.PORT),
                    config.get("username", SSHDefaults.USERNAME),
                    "agent",
                )
            )
        else:
            return segments("❌ 请输入 1、2 或 3 选择认证方式")

    elif step == "password":
        if context.current_group_id is not None:
            await context.end_session()
            return segments("❌ 已终止：密码认证配置只能在管理员私聊中完成")

        password_ref = f"passwords.{uuid.uuid4().hex}"
        await asyncio.to_thread(context.set_secret, password_ref, text)

        # 完成添加
        try:
            await manager.add_server(
                config["name"],
                config["host"],
                config.get("port", SSHDefaults.PORT),
                config.get("username", SSHDefaults.USERNAME),
                "password",
                password_ref=password_ref,
            )
        except Exception:
            await asyncio.to_thread(context.delete_secret, password_ref)
            raise

        await context.end_session()

        return segments(
            format_server_added(
                config["name"],
                config["host"],
                config.get("port", SSHDefaults.PORT),
                config.get("username", SSHDefaults.USERNAME),
                "password",
            )
        )

    elif step == "key_path":
        config["key_path"] = Path(os.path.expanduser(text)).as_posix()
        session.set("server_config", config)

        # 检查密钥文件是否存在
        if not os.path.exists(config["key_path"]):
            return segments(
                f"⚠️ 密钥文件不存在: {config['key_path']}\n\n请重新输入密钥路径，或确认文件位置:"
            )

        # 完成添加
        await manager.add_server(
            config["name"],
            config["host"],
            config.get("port", SSHDefaults.PORT),
            config.get("username", SSHDefaults.USERNAME),
            "key",
            key_path=config.get("key_path"),
        )

        await context.end_session()

        return segments(
            format_server_added(
                config["name"],
                config["host"],
                config.get("port", SSHDefaults.PORT),
                config.get("username", SSHDefaults.USERNAME),
                "key",
            )
        )

    return segments("❌ 未知状态，请重新开始添加")


async def _handle_connected_session(
    text: str, context: Context, session: Session, manager: SSHManager
) -> MessageSegments:
    """处理已连接状态的命令执行"""

    server_name = session.get(SessionKeys.SERVER_NAME)
    command_count = session.get(SessionKeys.COMMAND_COUNT, 0)

    # 检查连接状态
    is_valid, error_msg = await ensure_session_connected(context, session, manager)
    if not is_valid:
        return segments(error_msg)

    text = text.strip()

    # 获取 user_id
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)

    # 检查会话状态：是否正在执行命令
    if session.get(SessionKeys.STATE) == "executing":
        # 只有在执行状态下，才通过消息来判断是否停止
        if text.lower() in STOP_KEYWORDS:
            stop_result = await manager.stop_command(user_id, group_id, server_name)
            if not stop_result.found:
                return segments("⚠️ 未找到运行中的命令")
            if stop_result.remote_confirmed:
                return segments("🛑 远端命令已确认停止，命令通道已清理")
            if stop_result.local_cleaned:
                return segments("⚠️ 已关闭命令通道，但远端进程状态未知，请登录服务器确认")
            return segments("⚠️ 命令通道清理失败，且远端进程状态未知，请登录服务器确认")
        else:
            return segments("⏳ 有命令正在运行中...\n发送「停止」可强制结束，或等待命令完成。")

    # 处理退出命令 - 主动断开 SSH 连接
    # 框架会在用户输入退出命令时自动结束会话，但不会通知插件清理资源
    # 我们需要在这里拦截退出命令，先断开 SSH 连接，再让框架处理会话结束
    if text.lower() in EXIT_COMMANDS_SET:
        # 断开 SSH 连接
        manager.disconnect(user_id, group_id, server_name)
        logger.info("SSH audit operation=disconnect status=success")
        # 结束会话
        await context.end_session()
        return segments(f"👋 已断开与 {server_name} 的连接")

    # 特殊命令处理
    if text.lower() in {"/help", "ssh帮助", "插件帮助", "帮助"}:
        return segments(
            "🖥️ SSH 会话帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 直接输入命令执行\n"
            "💡 cd / export 命令会被记住\n"
            "💡 输入「状态」查看当前目录\n"
            "💡 输入「历史」查看命令历史\n"
            "💡 输入「!!」重复上一条命令\n"
            "💡 输入「showimg <文件名>」显示图片\n"
            "💡 输入「退出」/「取消」结束会话\n"
            "💡 输入「停止」中断运行中的命令\n"
            "━━━━━━━━━━━━━━━━━━\n"
        )

    if text.lower() in {"状态", "status"}:
        server = manager.get_server(server_name)
        cwd = session.get(SessionKeys.CWD, "~")
        env_vars = session.get(SessionKeys.ENV_VARS, {})
        info = f"已连接: {server_name}"
        if server:
            info += f"\n主机: {server['host']}"
        info += f"\n当前目录: {cwd}"
        if env_vars:
            info += f"\n环境变量: {len(env_vars)} 个"
        return segments(f"🖥️ {info}\n已执行命令: {command_count}")

    # 命令历史
    if text.lower() in {"历史", "history"}:
        history = session.get(SessionKeys.HISTORY, [])
        if not history:
            return segments("📜 命令历史为空")
        lines = ["📜 命令历史 (最近 20 条)", "━━━━━━━━━━━━━━━━━━"]
        for i, cmd in enumerate(history[-20:], 1):
            lines.append(f"{i:2d}. {cmd[:50]}{'...' if len(cmd) > 50 else ''}")
        return segments("\n".join(lines))

    # !! 重复上一条命令
    if text.strip() == "!!":
        history = session.get(SessionKeys.HISTORY, [])
        if history:
            text = history[-1]
        else:
            return segments("❌ 没有历史命令可重复")

    # showimg 命令 - 显示图片
    if text.strip().startswith("showimg "):
        return await _handle_showimg_command(text, context, session, manager)

    # === 开始执行命令 (后台流式) ===

    try:
        output_policy = SSHOutputPolicy.from_context(context)
    except ValueError as exc:
        return segments(f"❌ QingSSH 输出配置无效: {exc}")

    # 获取当前工作目录和环境变量
    cwd = session.get(SessionKeys.CWD, None)
    env_vars = session.get(SessionKeys.ENV_VARS, {})

    # 处理 export 命令 - 保存环境变量
    export_match = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text.strip())
    if export_match:
        var_name = export_match.group(1)
        var_value = export_match.group(2).strip('"').strip("'")
        # 验证环境变量名称，防止注入
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var_name):
            return segments("❌ 无效的环境变量名称")
        env_vars[var_name] = var_value
        session.set(SessionKeys.ENV_VARS, env_vars)

    # 检测是否为 cd 命令
    is_cd = is_cd_command(text)

    # 保存命令到历史
    history = session.get(SessionKeys.HISTORY, [])
    history.append(text)
    if len(history) > MAX_HISTORY_LENGTH:
        history = history[-MAX_HISTORY_LENGTH:]
    session.set(SessionKeys.HISTORY, history)

    # 构建实际执行的命令（统一路径处理）
    # cd 命令会自动附加 pwd 以获取绝对路径
    actual_command = build_command(text, cwd, env_vars)

    # 1. 设置状态为执行中
    session.set(SessionKeys.STATE, "executing")
    session.set(SessionKeys.COMMAND_COUNT, command_count + 1)

    # 只记录不可逆的审计摘要；原始命令仍完整交给可信管理员和 SSH 后端。
    command_audit = summarize_sensitive(actual_command)
    audit_job_id = uuid.uuid4().hex[:12]
    request_id = audit_request_id(context)
    logger.info(
        "SSH audit operation=command status=started request_id=%s job_id=%s "
        "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
        request_id,
        audit_job_id,
        command_audit.kind,
        command_audit.length,
        command_audit.byte_length,
        command_audit.fingerprint,
    )

    # 2. 启动后台任务（带超时保护）
    # 捕获发送动作所需的 ID
    target_user_id = context.current_user_id
    target_group_id = context.current_group_id

    # 检查是否有旧任务在运行，如果有则取消
    old_task = _get_session_task(context, session)
    if old_task and not old_task.done():
        old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)

    # 创建带超时的后台任务
    task = asyncio.create_task(
        _run_background_command(
            context,
            session,
            manager,
            server_name,
            actual_command,
            user_id,
            group_id,
            target_user_id,
            target_group_id,
            output_policy,
            is_cd=is_cd,
            audit_job_id=audit_job_id,
            request_id=request_id,
        )
    )

    _set_session_task(context, session, task)
    session.set(SessionKeys.CURRENT_TASK, "running")

    # 添加超时保护（可选，防止命令无限期挂起）
    # asyncio.create_task(asyncio.wait_for(task, timeout=COMMAND_TIMEOUT))

    return segments(f"🚀 命令已启动: {text}\n发送「停止」可中断...")


async def _run_background_command(
    context: Context,
    session: Session,
    manager: SSHManager,
    server_name: str,
    command: str,
    conn_user_id: str,  # 用来控制连接
    conn_group_id: str,  # 用来控制连接
    user_id: Any,  # 用来发消息
    group_id: Any,
    output_policy: SSHOutputPolicy,
    is_cd: bool = False,
    audit_job_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """
    后台运行 SSH 命令并流式推送消息

    Args:
        context: 插件上下文
        session: 会话对象
        manager: SSH 管理器
        server_name: 服务器名称
        command: 要执行的命令
        conn_user_id: 用于 SSH 连接隔离的用户 ID（字符串）
        conn_group_id: 用于 SSH 连接隔离的群 ID（字符串）
        user_id: 用于发送消息的用户 ID
        group_id: 用于发送消息的群 ID
        output_policy: QQ 投影、发送速率、归档与命令超时策略
        is_cd: 是否为 cd 命令（成功后需从 pwd 输出提取新的 CWD）
    """
    from core.plugin_base import build_action, segments

    cd_output_tail = ""
    relay: SSHOutputRelay | None = None
    relay_finished = False
    command_audit = summarize_sensitive(command)
    audit_job_id = audit_id(audit_job_id or uuid.uuid4().hex[:12])
    request_id = audit_id(request_id or audit_request_id(context))

    async def send_text(content: str) -> None:
        action = build_action(segments(content), user_id, group_id)
        if action:
            action["_bypass_sink"] = True
            await context.send_action(action)

    async def output_callback(text: Any) -> None:
        nonlocal cd_output_tail
        if is_cd:
            cd_output_tail = (cd_output_tail + str(text or ""))[-8192:]
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
                    session.set(SessionKeys.CWD, new_cwd)

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
                await relay.finish(f"❌ 命令执行出错: {exc}")
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
        # 恢复会话状态（检查会话是否还存在）
        try:
            if session and hasattr(session, "set"):
                session.set(SessionKeys.STATE, "connected")
                session.set(SessionKeys.CURRENT_TASK, None)
                _set_session_task(context, session, None)
        except Exception as exc:
            # 会话已结束或清理失败，记录日志但继续
            logger.debug("SSH session cleanup failed error_type=%s", audit_error_type(exc))


async def _handle_showimg_command(
    text: str, context: Context, session: Session, manager: SSHManager
) -> MessageSegments:
    """
    处理 showimg 命令 - 下载并显示图片

    用法: showimg <文件名或通配符>
    示例: showimg image.png
          showimg *.jpg
    """
    from core.plugin_base import build_action, image

    # 解析参数
    parts = text.strip().split(None, 1)
    if len(parts) < 2:
        return segments(
            "❌ 用法: showimg <文件名或通配符>\n示例: showimg image.png 或 showimg *.jpg"
        )

    file_pattern = parts[1].strip()

    # 获取服务器信息
    server_name = session.get(SessionKeys.SERVER_NAME)
    user_id = str(context.current_user_id)
    group_id = str(context.current_group_id)

    # 检查连接状态
    is_valid, error_msg = await ensure_session_connected(context, session, manager)
    if not is_valid:
        return segments(error_msg)

    # 获取当前工作目录（已经是绝对路径）
    cwd = session.get(SessionKeys.CWD, None)

    # 如果没有 CWD，通过 pwd 获取
    if not cwd:
        success, pwd_output = await manager.execute_command(user_id, group_id, server_name, "pwd")
        if success:
            cwd = pwd_output.strip()

    remote_dir = cwd or "."

    # 列出匹配的文件
    success, files = await manager.list_files(
        user_id, group_id, server_name, remote_dir, file_pattern
    )

    if not success or not files:
        return segments(f"❌ 未找到匹配的文件: {file_pattern}\n当前目录: {remote_dir}")

    # 过滤图片文件
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
    image_files = [f for f in files if Path(f).suffix.lower() in image_extensions]

    if not image_files:
        return segments(f"❌ 未找到图片文件\n匹配的文件: {', '.join(files)}")

    image_files = image_files[:MAX_SHOWIMG_FILES]

    # 创建本地保存目录
    images_dir = Path(context.plugin_dir) / "data" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 3600
    for stale in images_dir.iterdir():
        try:
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    # 下载图片并发送
    downloaded_files = []
    errors = []

    for filename in image_files:
        remote_path = resolve_remote_path(filename, cwd)

        # 构建本地路径（使用 UUID 避免文件名冲突）
        local_filename = f"{uuid.uuid4().hex}{Path(filename).suffix}"
        local_path = images_dir / local_filename

        # 下载文件
        success, message = await manager.download_file(
            user_id,
            group_id,
            server_name,
            remote_path,
            str(local_path),
            max_bytes=MAX_SHOWIMG_BYTES,
        )

        if success:
            downloaded_files.append((filename, local_path))
        else:
            errors.append(f"{filename}: {message}")

    # 构建消息
    message_segments = []

    if downloaded_files:
        message_segments.append(f"📷 已下载 {len(downloaded_files)} 张图片\n")

        # 发送图片消息
        for _filename, local_path in downloaded_files:
            img_segment = image(str(local_path))
            action = build_action([img_segment], context.current_user_id, context.current_group_id)
            if action:
                action["_bypass_sink"] = True
                await context.send_action(action)

    if errors:
        error_msg = f"\n❌ 下载失败 ({len(errors)} 个):\n" + "\n".join(
            f"  • {e}" for e in errors[:5]
        )
        if len(errors) > 5:
            error_msg += f"\n  ... 及其他 {len(errors) - 5} 个"
        message_segments.append(error_msg)

    for _filename, local_path in downloaded_files:
        local_path.unlink(missing_ok=True)

    return segments("".join(message_segments))
