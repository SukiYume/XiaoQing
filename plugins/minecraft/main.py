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

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from core.args import parse
from core.plugin_base import PluginContextProtocol, build_action, segments

# 使用相对导入
from . import connection, log_monitor, rcon

RconClient = rcon.RconClient
LogMonitor = log_monitor.LogMonitor
LogEventType = log_monitor.LogEventType
LogBatch = log_monitor.LogBatch
McConnection = connection.McConnection
ConnectionManager = connection.ConnectionManager

logger = logging.getLogger(__name__)

# 全局连接管理器
_manager = ConnectionManager()

MC_MAX_EVENTS_PER_CONNECTION = 12
MC_MAX_ACTION_CHARS = 1800
MC_MAX_ACTION_BYTES = 6000
MC_MAX_ACTIONS_PER_TICK = 5
MC_SEND_TIMEOUT_SECONDS = 3.0
MC_MONITOR_TIMEOUT_SECONDS = 3.0
MC_EVENT_BUCKET_CAPACITY = 24.0
MC_EVENT_BUCKET_REFILL_PER_SECOND = 0.5


@dataclass
class _EventTokenBucket:
    tokens: float = MC_EVENT_BUCKET_CAPACITY
    updated_at: float = 0.0

    def take(self, requested: int, *, now: float) -> int:
        if self.updated_at <= 0:
            self.updated_at = now
        else:
            elapsed = max(0.0, now - self.updated_at)
            self.tokens = min(
                MC_EVENT_BUCKET_CAPACITY,
                self.tokens + elapsed * MC_EVENT_BUCKET_REFILL_PER_SECOND,
            )
            self.updated_at = now
        granted = min(max(0, requested), int(self.tokens))
        self.tokens -= granted
        return granted


_event_buckets: dict[tuple[str, int], _EventTokenBucket] = {}

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("Minecraft plugin initialized")


async def shutdown(context: PluginContextProtocol | None) -> None:
    await _manager.cleanup_all()
    _event_buckets.clear()
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
    
    log_monitor_obj = None
    if log_file:
        log_path = Path(log_file)
        if not log_path.is_file():
            return segments("❌ 日志文件不存在或无法访问")
    
    # 创建 RCON 客户端
    try:
        rcon_client = RconClient(host, port, password)
        connected = await rcon_client.connect()
        if not connected:
            return segments("❌ RCON 连接失败，请检查地址和密码")
    except Exception as e:
        logger.error("MC RCON connection failed: %s", e)
        if "rcon_client" in locals():
            await rcon_client.disconnect()
        return segments(f"❌ 连接失败: {e}")
    
    # 创建日志监控器
    if log_file:
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
    await _manager.replace_connection(conn)
    
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
    
    await _manager.disconnect_connection(group_id, user_id)
    
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
        response = await conn.rcon_client.command(command)
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


def _server_bucket_key(conn: McConnection) -> tuple[str, int]:
    return (str(conn.host).strip().casefold(), int(conn.port))


def _normalize_log_batch(value: Any) -> LogBatch:
    if isinstance(value, LogBatch):
        return value
    events = list(value or [])
    return LogBatch(events=events, matched_total=len(events))


def _message_fits_budget(message: str) -> bool:
    return (
        len(message) <= MC_MAX_ACTION_CHARS
        and len(message.encode("utf-8", errors="replace")) <= MC_MAX_ACTION_BYTES
    )


def _batch_message(conn: McConnection, batch: LogBatch, *, now: float) -> tuple[str, int]:
    bucket = _event_buckets.setdefault(_server_bucket_key(conn), _EventTokenBucket())
    candidate_count = min(len(batch.events), MC_MAX_EVENTS_PER_CONNECTION)
    allowed = bucket.take(candidate_count, now=now)
    event_lines = [
        message
        for event in batch.events[:allowed]
        if (message := _format_event_message(event))
    ]
    dropped = max(0, batch.dropped_events) + max(0, len(batch.events) - len(event_lines))

    def render() -> str:
        lines = list(event_lines)
        if not lines:
            lines.append(f"🎮 [MC] {conn.host}:{conn.port} 日志摘要")
        if dropped:
            lines.append(f"⚠️ 另有 {dropped} 条 MC 事件被折叠/丢弃")
        if batch.skipped_bytes:
            line_text = "行数未知" if batch.skipped_lines is None else f"{batch.skipped_lines} 行"
            lines.append(
                f"⚠️ 日志积压跳过 {batch.skipped_bytes} 字节（{line_text}），仅处理有界 tail"
            )
        return "\n".join(lines)

    message = render()
    while event_lines and not _message_fits_budget(message):
        event_lines.pop()
        dropped += 1
        message = render()
    if not _message_fits_budget(message):
        message = (
            f"🎮 [MC] {conn.host}:{conn.port} 日志摘要\n"
            f"⚠️ 本轮事件过多，已折叠/丢弃至少 {max(1, dropped)} 条"
        )
    return message, max(batch.matched_total, len(batch.events)) + max(0, batch.skipped_lines or 0)


def _action_for_connection(conn: McConnection, message: str) -> dict[str, Any] | None:
    if conn.target_type == "group":
        return build_action(segments(message), None, conn.target_id)
    return build_action(segments(message), conn.target_id, None)


async def _send_mc_action(
    context: PluginContextProtocol,
    conn: McConnection,
    message: str,
) -> None:
    action = _action_for_connection(conn, message)
    if not action:
        return
    try:
        sent = await asyncio.wait_for(
            context.send_action(action),
            timeout=MC_SEND_TIMEOUT_SECONDS,
        )
        if sent is False:
            logger.warning("[MC] OneBot rejected log delivery for %s:%s", conn.host, conn.port)
    except asyncio.TimeoutError:
        logger.error("[MC] Log delivery timed out for %s:%s", conn.host, conn.port)
    except Exception as exc:
        logger.error("[MC] Log delivery failed for %s:%s: %s", conn.host, conn.port, exc)

async def scheduled(context: PluginContextProtocol) -> Optional[list[dict[str, Any]]]:
    """定时任务：检查所有连接的日志更新"""
    connections = _manager.all_connections()
    
    if not connections:
        return None
    
    active_bucket_keys = {_server_bucket_key(conn) for conn in connections}
    for key in list(_event_buckets):
        if key not in active_bucket_keys:
            _event_buckets.pop(key, None)

    deliveries: list[tuple[McConnection, str, int]] = []
    for conn in connections:
        if not conn.log_monitor:
            continue
        
        try:
            if hasattr(conn.log_monitor, "check_updates_async"):
                raw_batch = await asyncio.wait_for(
                    conn.log_monitor.check_updates_async(),
                    timeout=MC_MONITOR_TIMEOUT_SECONDS,
                )
            else:
                raw_batch = await asyncio.wait_for(
                    asyncio.to_thread(conn.log_monitor.check_updates),
                    timeout=MC_MONITOR_TIMEOUT_SECONDS,
                )
            batch = _normalize_log_batch(raw_batch)
            if not batch.events and not batch.dropped_events and not batch.skipped_bytes:
                continue
            message, event_total = _batch_message(conn, batch, now=time.monotonic())
            deliveries.append((conn, message, event_total))
        
        except Exception as e:
            logger.error("[MC] 处理连接 %s:%s 时出错: %s", conn.host, conn.port, e)

    if len(deliveries) <= MC_MAX_ACTIONS_PER_TICK:
        selected = deliveries
        overflow: list[tuple[McConnection, str, int]] = []
    else:
        selected = deliveries[: MC_MAX_ACTIONS_PER_TICK - 1]
        overflow = deliveries[MC_MAX_ACTIONS_PER_TICK - 1 :]

    for conn, message, _event_total in selected:
        logger.info("[MC] 批量转发到 %s_%s", conn.target_type, conn.target_id)
        await _send_mc_action(context, conn, message)

    if overflow:
        overflow_conn = overflow[0][0]
        overflow_events = sum(item[2] for item in overflow)
        notice = (
            f"🎮 [MC] 本轮达到 {MC_MAX_ACTIONS_PER_TICK} 条 QQ action 硬上限\n"
            f"⚠️ 另有 {len(overflow)} 个连接、{overflow_events} 条日志事件未转发"
        )
        await _send_mc_action(context, overflow_conn, notice)
    
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
