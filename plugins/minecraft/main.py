"""
Minecraft 服务器通信插件

功能：
1. 多服务器连接：不同的群/私聊可以连接不同的 MC 服务器
2. 双向聊天：QQ <-> Minecraft
3. 服务器状态查询

命令：
- /mc <消息> - 发送消息到 MC
- /mc status - 查询服务器状态
- /mc connect - 连接到 MC 服务器
- /mc disconnect - 断开当前连接
- /mc help - 显示帮助信息
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

from core.plugin_base import segments, PluginContextProtocol, build_action
from core.args import parse

# 使用相对导入
from . import rcon, log_monitor, connection

RconClient = rcon.RconClient
LogMonitor = log_monitor.LogMonitor
LogEventType = log_monitor.LogEventType
McConnection = connection.McConnection
ConnectionManager = connection.ConnectionManager

logger = logging.getLogger(__name__)

# 全局连接管理器
_manager = ConnectionManager()

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("Minecraft plugin initialized")


async def shutdown(context: PluginContextProtocol | None) -> None:
    await _manager.cleanup_all()
    logger.info("Minecraft plugin shutdown completed")

def _show_help() -> str:
    """
    显示 Minecraft 插件帮助信息
    """
    return (
        "🎮 Minecraft RCON 插件\n"
        "═══════════════════════\n\n"
        "📌 可用命令:\n\n"
        "1️⃣ /mc help\n"
        "   显示此帮助信息\n\n"
        "2️⃣ /mc connect <配置名> [log_file_path]\n"
        "   使用 plugins/minecraft/config.json 中的配置连接\n"
        "   示例: /mc connect default\n\n"
        "4️⃣ /mc disconnect\n"
        "   断开当前连接\n\n"
        "5️⃣ /mc status\n"
        "   查看连接状态\n\n"
        "6️⃣ /mc <command>\n"
        "   发送命令到服务器（连接后可用）\n"
        "   示例: /mc list, /mc time set day\n\n"
        "═══════════════════════"
    )


def _connect_usage() -> str:
    return (
        "用法:\n"
        "/mc connect <配置名> [log_file_path]\n\n"
        "示例:\n"
        "/mc connect default\n\n"
        "服务器配置请写在 plugins/minecraft/config.json 中"
    )


def _load_default_server(context: PluginContextProtocol, profile: str = "default") -> tuple[str, int, str, str] | None:
    """从 plugins/minecraft/config.json 读取服务器配置，避免密码通过聊天传递"""
    config_path = context.plugin_dir / "config.json"
    if not config_path.is_file():
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load minecraft config: %s", exc)
        return None

    server = config.get(profile)
    if not isinstance(server, dict):
        return None

    host = str(server.get("host", "")).strip()
    port_raw = server.get("port", 25575)
    password = str(server.get("password", "")).strip()
    log_file = str(server.get("log_file", "")).strip()

    if not host or not password:
        return None

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return None

    return host, port, password, log_file

# ============================================================
# 命令处理
# ============================================================

async def handle(command: str, args: str, event: dict[str, Any], context: PluginContextProtocol) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        group_id = event.get("group_id")
        user_id = event.get("user_id")
        parsed = parse(args)
        
        # 主 MC 命令使用统一入口
        if command.lower() in {"mc", "minecraft"}:
            # 如果没有参数，显示帮助
            if not parsed or not parsed.first:
                return segments(_show_help())
            
            # 检查子命令
            subcommand = parsed.first.lower()
            
            if subcommand in {"help", "帮助", "?"}:
                return segments(_show_help())
            elif subcommand in {"connect", "连接"}:
                return await _handle_connect(parsed.rest(1), group_id, user_id, context)
            elif subcommand in {"disconnect", "断开"}:
                return await _handle_disconnect(group_id, user_id, context)
            elif subcommand in {"status", "状态"}:
                return await _handle_status_command(group_id, user_id, context)
            else:
                # 默认：发送消息
                return await _handle_mc_message(args, event, context)
        
        # 兼容旧的独立命令（保持向后兼容）
        elif command.lower() in {"mcconnect", "mc连接"}:
            return await _handle_connect(args, group_id, user_id, context)
        elif command.lower() in {"mcdisconnect", "mc断开"}:
            return await _handle_disconnect(group_id, user_id, context)
        
        return segments("未知命令")
        
    except Exception as e:
        logger.exception("Minecraft handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

async def _handle_connect(
    args: str,
    group_id: Optional[int],
    user_id: Optional[int],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """
    处理连接命令

    格式: /mc connect <配置名> [log_file_path]
    服务器配置从 plugins/minecraft/config.json 读取，避免密码通过聊天传递
    """
    parts = args.strip().split()

    if user_id is None:
        return segments("❌ 无法识别用户信息")

    if not parts:
        return segments(_connect_usage())

    profile = parts[0]
    server = _load_default_server(context, profile)
    if server is None:
        return segments(
            f"❌ 未找到配置 '{profile}'，请在 plugins/minecraft/config.json 中添加\n"
            f"格式: {{\"{profile}\": {{\"host\": \"...\", \"port\": 25575, \"password\": \"...\"}}}}"
        )
    host, port, password, default_log_file = server
    log_file = parts[1] if len(parts) > 1 else default_log_file

    logger.info("MC connect request: host=%s, port=%d, user=%s", host, port, user_id)
    
    # 检查是否已有连接
    if _manager.has_connection(group_id, user_id):
        old_conn = _manager.get_connection(group_id, user_id)
        if old_conn:
            return segments(
                f"已连接到 {old_conn.host}:{old_conn.port}\n请先使用 /mc disconnect 断开"
            )
        return segments("已存在连接，请先使用 /mc disconnect 断开")
    
    # 创建 RCON 客户端
    try:
        rcon_client = RconClient(host, port, password)
        connected = await rcon_client.connect()
        if not connected:
            return segments("❌ RCON 连接失败，请检查地址和密码")
    except Exception as e:
        logger.error("MC RCON connection failed: %s", e)
        return segments(f"❌ 连接失败: {e}")
    
    # 创建日志监控器
    log_monitor_obj = None
    if log_file:
        log_path = Path(log_file)
        if log_path.name != "latest.log":
            return segments("❌ 日志文件必须是 latest.log")
        if not log_path.is_file():
            return segments("❌ 日志文件不存在或无法访问")
        log_monitor_obj = LogMonitor(str(log_path))
        if not log_monitor_obj.initialize():
            logger.warning("MC log file inaccessible: %s", log_file)
            log_monitor_obj = None
    
    # 确定目标
    target_type = "group" if group_id else "private"
    target_id = group_id if group_id else user_id
    
    # 保存连接
    conn = McConnection(
        host=host,
        port=port,
        password=password,
        log_file=log_file,
        target_type=target_type,
        target_id=target_id,
        rcon_client=rcon_client,
        log_monitor=log_monitor_obj,
    )
    _manager.add_connection(conn)
    
    log_status = "✅" if log_monitor_obj else "❌ (文件不存在或无法访问)"
    logger.info("MC connected: %s_%s -> %s:%s", target_type, target_id, host, port)
    
    return segments(f"✅ 已连接到 {host}:{port}\n📝 日志监控: {log_status}")

async def _handle_disconnect(
    group_id: Optional[int],
    user_id: Optional[int],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """处理断开连接命令"""
    if user_id is None:
        return segments("❌ 无法识别用户信息")
    
    if not _manager.has_connection(group_id, user_id):
        return segments("❌ 当前无连接")
    
    conn = _manager.get_connection(group_id, user_id)
    if conn:
        await conn.cleanup()
    _manager.remove_connection(group_id, user_id)
    
    logger.info("MC connection closed for user %s", user_id)
    return segments("✅ 已断开连接")

async def _handle_mc_message(args: str, event: dict[str, Any], context: PluginContextProtocol) -> list[dict[str, Any]]:
    """处理发送到 MC 服务器的命令"""
    group_id = event.get("group_id")
    user_id = event.get("user_id")
    
    if user_id is None:
        return segments("❌ 无法识别用户信息")
    
    if not _manager.has_connection(group_id, user_id):
        return segments("❌ 未连接到服务器，请先使用 /mc connect 连接")
    
    conn = _manager.get_connection(group_id, user_id)
    if not conn or not conn.rcon_client:
        return segments("❌ 连接无效")
    
    command = args.strip()
    if not command:
        return segments("❌ 请提供要执行的命令")
    
    try:
        response = await conn.rcon_client.send_command(command)
        logger.info("MC command executed: %s", command)
        if response:
            return segments(f"📤 {response}")
        return segments("✅ 命令已发送（无返回）")
    except Exception as e:
        logger.error("MC command execution failed: %s", e)
        return segments(f"❌ 命令执行失败: {e}")

async def _handle_status_command(
    group_id: Optional[int],
    user_id: Optional[int],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """处理状态查询命令"""
    if user_id is None:
        return segments("❌ 无法识别用户信息")
    
    if not _manager.has_connection(group_id, user_id):
        return segments("❌ 未连接到任何服务器")
    
    conn = _manager.get_connection(group_id, user_id)
    if not conn:
        return segments("❌ 连接信息获取失败")
    
    log_status = "✅ 正常" if conn.log_monitor else "❌ 未启用"
    logger.debug("MC status checked: %s:%d", conn.host, conn.port)
    return segments(
        f"📊 连接状态\n"
        f"服务器: {conn.host}:{conn.port}\n"
        f"日志监控: {log_status}"
    )

# ============================================================
# 定时任务
# ============================================================

async def scheduled(context: PluginContextProtocol) -> Optional[list[dict[str, Any]]]:
    """定时任务：检查所有连接的日志更新"""
    connections = _manager.all_connections()
    
    if not connections:
        return None
    
    for conn in connections:
        if not conn.log_monitor:
            continue
        
        try:
            events = conn.log_monitor.check_updates()
            
            for event in events:
                message = _format_event_message(event)
                
                if message:
                    logger.info(
                        "[MC] 转发到 %s_%s: %s",
                        conn.target_type,
                        conn.target_id,
                        message,
                    )
                    
                    if conn.target_type == "group":
                        action = build_action(segments(message), None, conn.target_id)
                    else:
                        action = build_action(segments(message), conn.target_id, None)
                    
                    if action:
                        await context.send_action(action)
        
        except Exception as e:
            logger.error("[MC] 处理连接 %s:%s 时出错: %s", conn.host, conn.port, e)
    
    return None

def _format_event_message(event) -> Optional[str]:
    """格式化日志事件为消息"""
    if event.event_type == LogEventType.CHAT:
        return f"🎮 [MC] {event.player}: {event.message}"
    
    elif event.event_type == LogEventType.JOIN:
        return f"🎮 {event.player} 加入了游戏"
    
    elif event.event_type == LogEventType.LEAVE:
        return f"🎮 {event.player} 离开了游戏"
    
    elif event.event_type == LogEventType.DEATH:
        return f"💀 {event.player} {event.message}"
    
    elif event.event_type == LogEventType.ADVANCEMENT:
        return f"🏆 {event.player} 获得成就 [{event.message}]"
    
    return None
