"""
SSH 会话处理器

处理多轮对话中的会话消息（添加服务器流程、命令执行）

退出命令处理：
框架支持的退出命令：{"退出", "取消", "exit", "quit", "q"}
插件会拦截这些命令，先断开 SSH 连接再结束会话，避免连接泄露。
"""

import logging
import os
import re
import asyncio
import uuid
from pathlib import Path
from typing import Any

from core.plugin_base import segments
from core.constants import EXIT_COMMANDS_SET

from .config import (
    CANCEL_KEYWORDS,
    STOP_KEYWORDS,
    STREAM_BUFFER_SIZE,
    STREAM_FLUSH_INTERVAL,
    EXIT_CODE_INTERRUPTED,
    SessionKeys,
    SSHDefaults,
    MAX_HISTORY_LENGTH,
    COMMAND_TIMEOUT,
)
from .path_resolver import is_cd_command, build_command, extract_cwd_from_output, resolve_remote_path
from .ssh_manager import SSHManager, get_manager
from .validators import validate_server_name, validate_hostname, validate_port
from .message_formatter import format_server_added
from .types import Context, Session, OneBotEvent, MessageSegments

logger = logging.getLogger(__name__)

_SESSION_TASKS: dict[str, asyncio.Task[Any]] = {}


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

async def ensure_session_connected(context: Context, session: Session, manager: SSHManager) -> tuple[bool, str]:
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
    text: str,
    context: Context,
    session: Session,
    manager: SSHManager
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
            
            return segments(format_server_added(
                config["name"],
                config["host"],
                config.get("port", SSHDefaults.PORT),
                config.get("username", SSHDefaults.USERNAME),
                "agent"
            ))
        else:
            return segments("❌ 请输入 1、2 或 3 选择认证方式")
    
    elif step == "password":
        config["password"] = text
        session.set("server_config", config)
        
        # 完成添加
        await manager.add_server(
            config["name"],
            config["host"],
            config.get("port", SSHDefaults.PORT),
            config.get("username", SSHDefaults.USERNAME),
            "password",
            password=config.get("password"),
        )
        
        await context.end_session()
        
        return segments(format_server_added(
            config["name"],
            config["host"],
            config.get("port", SSHDefaults.PORT),
            config.get("username", SSHDefaults.USERNAME),
            "password"
        ))
    
    elif step == "key_path":
        config["key_path"] = Path(os.path.expanduser(text)).as_posix()
        session.set("server_config", config)
        
        # 检查密钥文件是否存在
        if not os.path.exists(config["key_path"]):
            return segments(f"⚠️ 密钥文件不存在: {config['key_path']}\n\n请重新输入密钥路径，或确认文件位置:")
        
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
        
        return segments(format_server_added(
            config["name"],
            config["host"],
            config.get("port", SSHDefaults.PORT),
            config.get("username", SSHDefaults.USERNAME),
            "key"
        ))
    
    return segments("❌ 未知状态，请重新开始添加")

async def _handle_connected_session(
    text: str,
    context: Context,
    session: Session,
    manager: SSHManager
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
            if manager.stop_command(user_id, group_id, server_name):
                return segments("🛑 正在发送停止信号...")
            else:
                return segments("⚠️ 未找到运行中的命令")
        else:
            return segments("⏳ 有命令正在运行中...\n发送「停止」可强制结束，或等待命令完成。")

    # 处理退出命令 - 主动断开 SSH 连接
    # 框架会在用户输入退出命令时自动结束会话，但不会通知插件清理资源
    # 我们需要在这里拦截退出命令，先断开 SSH 连接，再让框架处理会话结束
    if text.lower() in EXIT_COMMANDS_SET:
        # 断开 SSH 连接
        manager.disconnect(user_id, group_id, server_name)
        logger.info("SSH connection disconnected on session exit: user=%s, server=%s", user_id, server_name)
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
    
    # 获取当前工作目录和环境变量
    cwd = session.get(SessionKeys.CWD, None)
    env_vars = session.get(SessionKeys.ENV_VARS, {})
    
    # 处理 export 命令 - 保存环境变量
    export_match = re.match(r'^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$', text.strip())
    if export_match:
        var_name = export_match.group(1)
        var_value = export_match.group(2).strip('"').strip("'")
        # 验证环境变量名称，防止注入
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', var_name):
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
    
    # 记录命令执行
    logger.info(
        "SSH command execution: user=%s, server=%s, command=%s",
        user_id, server_name, text[:100]
    )
    
    # 2. 启动后台任务（带超时保护）
    # 捕获发送动作所需的 ID
    target_user_id = context.current_user_id
    target_group_id = context.current_group_id

    # 检查是否有旧任务在运行，如果有则取消
    old_task = _get_session_task(context, session)
    if old_task and not old_task.done():
        old_task.cancel()

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
            is_cd=is_cd,
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
    conn_group_id: str, # 用来控制连接
    user_id: Any,  # 用来发消息
    group_id: Any,
    is_cd: bool = False,
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
        is_cd: 是否为 cd 命令（成功后需从 pwd 输出提取新的 CWD）
    """
    from core.plugin_base import build_action, segments
    import time
    
    buffer = []
    cd_output = []  # 用于 cd 命令捕获 pwd 输出
    last_send_time = time.time()

    async def send_buffer_content(content: str):
        """发送缓冲区内容（长消息拆分由 core 统一处理）"""
        if not content:
            return
        action = build_action(segments(content), user_id, group_id)
        if action:
            action["_bypass_sink"] = True
            await context.send_action(action)
    
    async def flush_buffer(force=False):
        nonlocal last_send_time
        current_time = time.time()
        
        # 触发条件：强制，或者缓冲区有内容且 (内容太长 或 时间间隔够久)
        if not buffer:
            return
            
        if force or len(buffer) > STREAM_BUFFER_SIZE or (current_time - last_send_time > STREAM_FLUSH_INTERVAL):
            # 合并消息
            content = "".join(buffer)
            buffer.clear()
            last_send_time = current_time
            
            await send_buffer_content(content)
    
    async def output_callback(text):
        buffer.append(text)
        if is_cd:
            cd_output.append(text)
        await flush_buffer(force=False)
        
    # 执行命令
    try:
        exit_code = await manager.execute_command_stream(
            conn_user_id,
            conn_group_id,
            server_name, 
            command, 
            output_callback
        )
        
        # 发送剩余 buffer
        await flush_buffer(force=True)
        
        # 发送结束状态
        result_msg = ""
        if exit_code == EXIT_CODE_INTERRUPTED:
            result_msg = "\n⏹️ 命令已中断"
        elif exit_code != 0:
            result_msg = f"\n⚠️ 命令失败 [退出码: {exit_code}]"
        else:
            result_msg = "\n✅ 命令执行完毕"
            # cd 命令成功后，从 pwd 输出中提取绝对路径作为新的 CWD
            if is_cd:
                new_cwd = extract_cwd_from_output("".join(cd_output))
                if new_cwd:
                    session.set(SessionKeys.CWD, new_cwd)
            
        action = build_action(segments(result_msg), user_id, group_id)
        if action:
            action["_bypass_sink"] = True
            await context.send_action(action)
            
    except Exception as e:
        logger.exception("Background SSH command error: %s", e)
        
        # 发送错误消息
        action = build_action(segments(f"\n❌ 命令执行出错: {e}"), user_id, group_id)
        if action:
            action["_bypass_sink"] = True
            await context.send_action(action)
    finally:
        # 恢复会话状态（检查会话是否还存在）
        try:
            if session and hasattr(session, 'set'):
                session.set(SessionKeys.STATE, "connected")
                session.set(SessionKeys.CURRENT_TASK, None)
                _set_session_task(context, session, None)
        except Exception as e:
            # 会话已结束或清理失败，记录日志但继续
            logger.debug("Session cleanup error: %s", e)

async def _handle_showimg_command(
    text: str,
    context: Context,
    session: Session,
    manager: SSHManager
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
        return segments("❌ 用法: showimg <文件名或通配符>\n示例: showimg image.png 或 showimg *.jpg")
    
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
    success, files = await manager.list_files(user_id, group_id, server_name, remote_dir, file_pattern)
    
    if not success or not files:
        return segments(f"❌ 未找到匹配的文件: {file_pattern}\n当前目录: {remote_dir}")
    
    # 过滤图片文件
    image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg'}
    image_files = [f for f in files if Path(f).suffix.lower() in image_extensions]
    
    if not image_files:
        return segments(f"❌ 未找到图片文件\n匹配的文件: {', '.join(files)}")
    
    # 创建本地保存目录
    images_dir = Path(context.plugin_dir) / "data" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    
    # 下载图片并发送
    downloaded_files = []
    errors = []
    
    for filename in image_files:
        remote_path = resolve_remote_path(filename, cwd)
        
        # 构建本地路径（使用 UUID 避免文件名冲突）
        local_filename = f"{uuid.uuid4().hex}{Path(filename).suffix}"
        local_path = images_dir / local_filename
        
        # 下载文件
        success, message = await manager.download_file(user_id, group_id, server_name, remote_path, str(local_path))
        
        if success:
            downloaded_files.append((filename, local_path))
        else:
            errors.append(f"{filename}: {message}")
    
    # 构建消息
    message_segments = []
    
    if downloaded_files:
        message_segments.append(f"📷 已下载 {len(downloaded_files)} 张图片\n")
        
        # 发送图片消息
        for filename, local_path in downloaded_files:
            img_segment = image(str(local_path))
            action = build_action([img_segment], context.current_user_id, context.current_group_id)
            if action:
                action["_bypass_sink"] = True
                await context.send_action(action)
    
    if errors:
        error_msg = f"\n❌ 下载失败 ({len(errors)} 个):\n" + "\n".join(f"  • {e}" for e in errors[:5])
        if len(errors) > 5:
            error_msg += f"\n  ... 及其他 {len(errors) - 5} 个"
        message_segments.append(error_msg)
    
    return segments("".join(message_segments))
