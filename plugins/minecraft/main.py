"""通过 RCON 管理 Minecraft，并把有界日志事件转发到对应 QQ 目标。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from core.args import parse
from core.delivery import DeliveryReceipt, send_with_receipt
from core.interfaces import DeliveryTarget, PluginContextProtocol
from core.plugin_base import Segments, bounded_external_text, build_action, run_sync, segments
from core.sensitive_audit import summarize_sensitive

from .audit import audit_error_type, audit_request_id
from .connection import ConnectionManager, McConnection
from .log_monitor import LogBatch, LogEvent, LogEventType, LogMonitor
from .rcon import RconClient

logger = logging.getLogger(__name__)

# QQ 投递与日志洪泛边界。
MC_MAX_EVENTS_PER_CONNECTION = 12
MC_MAX_ACTION_CHARS = 1800
MC_MAX_ACTION_BYTES = 6000
MC_MAX_ACTIONS_PER_TICK = 5
MC_SEND_TIMEOUT_SECONDS = 3.0
MC_EVENT_BUCKET_CAPACITY = 24.0
MC_EVENT_BUCKET_REFILL_PER_SECOND = 0.5

# 配置、命令响应和单条日志字段边界。
MC_MAX_CONFIG_BYTES = 64 * 1024
MC_MAX_RESPONSE_CHARS = 4000
MC_MAX_RESPONSE_BYTES = 12 * 1024
MC_MAX_EVENT_FIELD_CHARS = 600
MC_MAX_EVENT_FIELD_BYTES = 2400

_PROFILE_PATTERN = re.compile(r"[\w.-]{1,64}\Z")


@dataclass(frozen=True, slots=True)
class _ServerConfig:
    host: str
    port: int
    password: str
    log_file: str


class _MinecraftConfigError(ValueError):
    """可直接返回给管理员的固定配置错误。"""


@dataclass(slots=True)
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

    def refund(self, granted: int) -> None:
        self.tokens = min(MC_EVENT_BUCKET_CAPACITY, self.tokens + max(0, granted))


_manager = ConnectionManager()
_event_buckets: dict[tuple[str, int, str, int], _EventTokenBucket] = {}
_delivery_cursor = 0
_schedule_lock = asyncio.Lock()


def init(_context: PluginContextProtocol | None = None) -> None:
    """初始化由模块级轻量状态完成，此钩子只记录生命周期。"""

    logger.info("Minecraft plugin initialized")


async def shutdown(_context: PluginContextProtocol | None) -> None:
    """关闭所有 RCON 连接并清空仅在进程内有效的限流状态。"""

    global _delivery_cursor
    async with _schedule_lock:
        await _manager.cleanup_all()
        _event_buckets.clear()
        _delivery_cursor = 0
    logger.info("Minecraft plugin shutdown completed")


def _show_help() -> str:
    return (
        "🎮 Minecraft RCON 插件\n"
        "═══════════════════════\n\n"
        "1️⃣ /mc help\n"
        "   显示此帮助信息\n\n"
        "2️⃣ /mc connect <配置名>\n"
        "   读取本地服务器配置和 config/secrets.json 中的密钥\n\n"
        "3️⃣ /mc disconnect\n"
        "   断开当前私聊的连接\n\n"
        "4️⃣ /mc status\n"
        "   查看当前连接和日志监控状态\n\n"
        "5️⃣ /mc <服务器命令>\n"
        "   连接后执行 RCON 命令，例如 /mc list\n\n"
        "6️⃣ /mc say <消息>\n"
        "   向所有在线玩家广播，例如 /mc say 大家好\n\n"
        "7️⃣ /mc tell <玩家名> <消息>\n"
        "   向指定玩家发送私信\n\n"
        "💬 日志监控启用后，玩家聊天、加入和离开等事件会转发到当前 QQ 私聊。\n\n"
        "═══════════════════════"
    )


def _connect_usage() -> str:
    return (
        "用法: /mc connect <配置名>\n"
        "示例: /mc connect default\n\n"
        "主机、端口和可选日志路径写在 plugins/minecraft/config.json；密码写在"
        " config/secrets.json 的 plugins.minecraft.<配置名> 中。"
    )


def _target_from_event(event: dict[str, Any]) -> DeliveryTarget | None:
    """群事件严格使用群目标；没有群 ID 时才使用私聊用户目标。"""

    group_id = event.get("group_id")
    user_id = event.get("user_id")
    try:
        if group_id is not None:
            return DeliveryTarget("group", group_id)
        if user_id is not None:
            return DeliveryTarget("private", user_id)
    except (TypeError, ValueError):
        return None
    return None


def _read_config_root(config_path: Path) -> dict[str, Any]:
    """有界读取配置文件，并把 I/O/JSON 细节收敛为固定公开错误。"""

    try:
        if not config_path.is_file():
            raise _MinecraftConfigError("未找到 config.json")
        if config_path.stat().st_size > MC_MAX_CONFIG_BYTES:
            raise _MinecraftConfigError("config.json 超过 64 KiB 安全上限")
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except _MinecraftConfigError:
        raise
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        logger.warning("Minecraft config load failed error_type=%s", audit_error_type(exc))
        raise _MinecraftConfigError("config.json 无法读取或不是有效 JSON") from None

    if not isinstance(root, dict):
        raise _MinecraftConfigError("config.json 顶层必须是对象")
    return root


def _validate_server_config(server: dict[str, Any]) -> _ServerConfig:
    """校验一个 profile 的公开连接字段和已解析的密钥。"""

    host = server.get("host")
    port = server.get("port", 25575)
    log_file = server.get("log_file", "")
    password = server.get("_resolved_password")
    if "password" in server:
        raise _MinecraftConfigError(
            "服务器 password 必须写入 config/secrets.json 的 plugins.minecraft.<配置名>"
        )
    if (
        not isinstance(host, str)
        or not host
        or host != host.strip()
        or len(host) > 253
        or any(char.isspace() or ord(char) < 32 for char in host)
    ):
        raise _MinecraftConfigError("服务器 host 无效")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise _MinecraftConfigError("服务器 port 必须是 1 至 65535 的整数")
    if (
        not isinstance(password, str)
        or not password
        or "\0" in password
        or len(password.encode("utf-8")) > 4096
    ):
        raise _MinecraftConfigError("服务器 password 为空、含 NUL 或超过 4096 字节")
    if (
        not isinstance(log_file, str)
        or "\0" in log_file
        or len(log_file) > 4096
        or log_file != log_file.strip()
    ):
        raise _MinecraftConfigError("服务器 log_file 无效")
    return _ServerConfig(host, port, password, log_file.strip())


def _load_server_config(context: PluginContextProtocol, profile: str) -> _ServerConfig:
    """读取公开 profile，并从同一份 settings 快照解析其私有 RCON 密钥。"""

    if _PROFILE_PATTERN.fullmatch(profile) is None:
        raise _MinecraftConfigError("配置名必须为 1 至 64 个字母、数字、下划线、点或连字符")
    root = _read_config_root(context.plugin_dir / "config.json")
    server = root.get(profile)
    if not isinstance(server, dict):
        raise _MinecraftConfigError("未找到指定服务器配置")
    settings = context.get_settings_snapshot()
    if settings.secrets_status != "valid":
        raise _MinecraftConfigError("Minecraft 密钥配置当前不可用，请检查 config/secrets.json")
    password = settings.plugin_secrets("minecraft").get(profile)
    if password is None:
        raise _MinecraftConfigError(
            "未找到 config/secrets.json 中的 Minecraft 服务器密钥"
        )
    return _validate_server_config({**server, "_resolved_password": password})


def _create_log_monitor(
    server: _ServerConfig,
    target: DeliveryTarget,
    context: PluginContextProtocol,
) -> LogMonitor | None:
    if not server.log_file:
        return None
    try:
        configured_path = Path(server.log_file).expanduser()
        log_path = (
            configured_path
            if configured_path.is_absolute()
            else context.plugin_dir / configured_path
        )
        if not log_path.is_file():
            logger.warning("Minecraft log monitor unavailable reason=not_file")
            return None

        resolved_path = log_path.resolve(strict=True)
        cursor_material = f"{resolved_path}\0{target.kind}\0{target.target_id}".encode(
            "utf-8",
            errors="surrogatepass",
        )
        cursor_name = hashlib.sha256(cursor_material).hexdigest() + ".json"
        monitor = LogMonitor(
            str(resolved_path),
            state_path=context.data_dir / "log_cursors" / cursor_name,
        )
        return monitor if monitor.initialize() else None
    except Exception as exc:
        logger.warning(
            "Minecraft log monitor unavailable error_type=%s",
            audit_error_type(exc),
        )
        return None


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """规范化顶层命令，并把需要连接身份的分支交给专用处理函数。"""

    try:
        return await _dispatch_command(command, args, event, context)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Minecraft handle failed error_type=%s", audit_error_type(exc))
        return segments("❌ 处理 Minecraft 请求失败，请稍后重试")


async def _dispatch_command(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    normalized_command = command.casefold()
    if normalized_command in {"mc", "minecraft"}:
        return await _dispatch_mc_command(args, event, context)

    target = _target_from_event(event)
    if target is None:
        return segments("❌ 无法识别有效的群或用户目标")
    if normalized_command in {"mcconnect", "mc连接"}:
        return await _handle_connect(args, target, context)
    if normalized_command in {"mcdisconnect", "mc断开"}:
        if args.strip():
            return segments("用法: /mcdisconnect")
        return await _handle_disconnect(target, context)
    return segments("未知命令")


async def _dispatch_mc_command(
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    parsed = parse(args)
    if not parsed.first:
        return segments(_show_help())
    if parsed.first.casefold() in {"help", "帮助", "?"}:
        if len(parsed) != 1 or parsed.options:
            return segments("用法: /mc help")
        return segments(_show_help())
    target = _target_from_event(event)
    if target is None:
        return segments("❌ 无法识别有效的群或用户目标")
    subcommand = parsed.first.casefold()
    if subcommand in {"connect", "连接"}:
        return await _handle_connect(parsed.rest(1), target, context)
    if subcommand in {"disconnect", "断开"}:
        if len(parsed) != 1 or parsed.options:
            return segments("用法: /mc disconnect")
        return await _handle_disconnect(target, context)
    if subcommand in {"status", "状态"}:
        if len(parsed) != 1 or parsed.options:
            return segments("用法: /mc status")
        return _handle_status_command(target, context)
    return await _handle_mc_command(args, target, context)


async def _handle_connect(
    args: str,
    target: DeliveryTarget,
    context: PluginContextProtocol,
) -> Segments:
    profile = args.strip()
    if not profile:
        return segments(_connect_usage())
    try:
        server = _load_server_config(context, profile)
    except _MinecraftConfigError as exc:
        return segments(f"❌ {exc}")

    target_audit = summarize_sensitive("\0".join((server.host, str(server.port), server.log_file)))
    request_id = audit_request_id(context)
    logger.info(
        "sensitive_audit operation=minecraft.connect request_id=%s status=started "
        "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
        request_id,
        target_audit.kind,
        target_audit.length,
        target_audit.byte_length,
        target_audit.fingerprint,
    )

    try:
        client = RconClient(server.host, server.port, server.password)
        connect_result = await client.connect()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Minecraft connect status=failed request_id=%s error_type=%s",
            request_id,
            audit_error_type(exc),
        )
        return segments("❌ RCON 连接初始化失败")
    if not connect_result.success:
        logger.warning(
            "Minecraft connect status=failed request_id=%s error_kind=%s",
            request_id,
            connect_result.error_kind.value if connect_result.error_kind else "unknown",
        )
        return segments(f"❌ {connect_result.error_message}")

    monitor = _create_log_monitor(server, target, context)
    await _manager.replace_connection(
        McConnection(
            host=server.host,
            port=server.port,
            target=target,
            rcon_client=client,
            log_monitor=monitor,
        )
    )
    logger.info(
        "sensitive_audit operation=minecraft.connect request_id=%s status=success "
        "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s",
        request_id,
        target_audit.kind,
        target_audit.length,
        target_audit.byte_length,
        target_audit.fingerprint,
    )

    if monitor is not None:
        log_status = "✅ 已启用"
    elif server.log_file:
        log_status = "⚠️ 配置路径不可用，已仅连接 RCON"
    else:
        log_status = "未配置"
    return segments(f"✅ 已连接到 {server.host}:{server.port}\n📝 日志监控: {log_status}")


async def _handle_disconnect(
    target: DeliveryTarget,
    context: PluginContextProtocol,
) -> Segments:
    disconnected = await _manager.disconnect_connection(target)
    if disconnected is None:
        return segments("❌ 当前无连接")
    logger.info(
        "Minecraft audit operation=disconnect request_id=%s status=success",
        audit_request_id(context),
    )
    return segments("✅ 已断开连接")


async def _handle_mc_command(
    args: str,
    target: DeliveryTarget,
    context: PluginContextProtocol,
) -> Segments:
    conn = _manager.get_connection(target)
    if conn is None:
        return segments("❌ 未连接到服务器，请先使用 /mc connect 连接")
    if conn.rcon_client is None:
        return segments("❌ RCON 连接已关闭，请重新连接")

    command = args.strip()
    if not command:
        return segments("❌ 请提供要执行的命令")
    command_audit = summarize_sensitive(command)
    request_id = audit_request_id(context)
    try:
        result = await conn.rcon_client.command(command)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "sensitive_audit operation=minecraft.command request_id=%s status=failed "
            "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
            "error_type=%s",
            request_id,
            command_audit.kind,
            command_audit.length,
            command_audit.byte_length,
            command_audit.fingerprint,
            audit_error_type(exc),
        )
        return segments("❌ RCON 命令执行失败，请重新连接后重试")

    if not result.success:
        logger.warning(
            "sensitive_audit operation=minecraft.command request_id=%s status=failed "
            "error_kind=%s payload_kind=%s payload_length=%d payload_bytes=%d "
            "payload_fingerprint=%s",
            request_id,
            result.error_kind.value if result.error_kind else "unknown",
            command_audit.kind,
            command_audit.length,
            command_audit.byte_length,
            command_audit.fingerprint,
        )
        return segments(f"❌ {result.error_message}")

    response_audit = summarize_sensitive(result.response)
    logger.info(
        "sensitive_audit operation=minecraft.command request_id=%s status=success "
        "payload_kind=%s payload_length=%d payload_bytes=%d payload_fingerprint=%s "
        "response_kind=%s response_length=%d response_bytes=%d response_fingerprint=%s",
        request_id,
        command_audit.kind,
        command_audit.length,
        command_audit.byte_length,
        command_audit.fingerprint,
        response_audit.kind,
        response_audit.length,
        response_audit.byte_length,
        response_audit.fingerprint,
    )
    if not result.response:
        return segments("✅ 命令执行成功（空响应）")
    truncation_warning = (
        "\n⚠️ 响应可能不完整（续包等待超时）" if result.truncated else ""
    )
    response = bounded_external_text(
        result.response,
        max_chars=max(1, MC_MAX_RESPONSE_CHARS - len(truncation_warning)),
        max_bytes=max(1, MC_MAX_RESPONSE_BYTES - len(truncation_warning.encode("utf-8"))),
        suffix="\n…（响应已截断）",
        strip=False,
    )
    return segments(f"📤 {response}{truncation_warning}")


def _handle_status_command(
    target: DeliveryTarget,
    context: PluginContextProtocol,
) -> Segments:
    conn = _manager.get_connection(target)
    if conn is None:
        return segments("❌ 未连接到任何服务器")
    log_status = "✅ 正常" if conn.log_monitor is not None else "未启用"
    logger.debug(
        "Minecraft audit operation=status request_id=%s status=success",
        audit_request_id(context),
    )
    return segments(f"📊 连接状态\n服务器: {conn.host}:{conn.port}\n日志监控: {log_status}")


def _server_bucket_key(conn: McConnection) -> tuple[str, int, str, int]:
    return (
        conn.host.casefold(),
        conn.port,
        conn.target.kind,
        conn.target.target_id,
    )


def _message_fits_budget(message: str) -> bool:
    return (
        len(message) <= MC_MAX_ACTION_CHARS and len(message.encode("utf-8")) <= MC_MAX_ACTION_BYTES
    )


def _format_event_message(event: LogEvent) -> str:
    player = bounded_external_text(
        event.player,
        max_chars=16,
        max_bytes=64,
        suffix="",
        strip=False,
    )
    message = bounded_external_text(
        event.message or "",
        max_chars=MC_MAX_EVENT_FIELD_CHARS,
        max_bytes=MC_MAX_EVENT_FIELD_BYTES,
        suffix="…",
        strip=False,
    )
    if event.event_type is LogEventType.CHAT:
        return f"🎮 [MC] {player}: {message}"
    if event.event_type is LogEventType.JOIN:
        return f"🎮 {player} 加入了游戏"
    if event.event_type is LogEventType.LEAVE:
        return f"🎮 {player} 离开了游戏"
    if event.event_type is LogEventType.DEATH:
        return f"💀 {player} {message}"
    return f"🏆 {player} 获得成就 [{message}]"


def _batch_message(
    conn: McConnection,
    batch: LogBatch,
    *,
    now: float,
) -> tuple[str, int, int]:
    bucket = _event_buckets.setdefault(_server_bucket_key(conn), _EventTokenBucket())
    candidate_count = min(len(batch.events), MC_MAX_EVENTS_PER_CONNECTION)
    granted = bucket.take(candidate_count, now=now)
    event_lines = [_format_event_message(event) for event in batch.events[:granted]]
    dropped = max(0, batch.dropped_events) + max(0, len(batch.events) - granted)

    def render() -> str:
        lines = list(event_lines) or [f"🎮 [MC] {conn.host}:{conn.port} 日志摘要"]
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
    return message, max(batch.matched_total, len(batch.events)), granted


def _action_for_connection(conn: McConnection, message: str) -> dict[str, Any]:
    # DeliveryTarget 与非空文本已在上游验证，因此 build_action 此处不可能返回 None。
    return cast(
        dict[str, Any],
        build_action(segments(message), conn.target.user_id, conn.target.group_id),
    )


async def _send_mc_action(
    context: PluginContextProtocol,
    conn: McConnection,
    message: str,
    receipt: DeliveryReceipt,
) -> None:
    action = _action_for_connection(conn, message)
    try:
        outcome = await asyncio.wait_for(
            send_with_receipt(context.send_action, action, receipt),
            timeout=MC_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await receipt.record(None)
        logger.warning(
            "Minecraft log delivery status=unknown reason=timeout target_type=%s",
            conn.target.kind,
        )
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await receipt.record(False)
        logger.error(
            "Minecraft log delivery status=failed target_type=%s error_type=%s",
            conn.target.kind,
            audit_error_type(exc),
        )
        return
    if outcome is False:
        logger.warning(
            "Minecraft log delivery status=rejected target_type=%s",
            conn.target.kind,
        )
    elif outcome is None:
        logger.warning(
            "Minecraft log delivery status=unknown target_type=%s",
            conn.target.kind,
        )


def _commit_log_batch(conn: McConnection, batch: LogBatch) -> bool:
    if conn.log_monitor is None or batch.cursor_before is None:
        return False
    try:
        committed = conn.log_monitor.commit(batch)
    except Exception as exc:
        logger.error(
            "Minecraft log cursor commit status=failed error_type=%s",
            audit_error_type(exc),
        )
        return False
    if not committed:
        logger.error("Minecraft log cursor commit status=rejected reason=stale_batch")
    return bool(committed)


async def _poll_connection(conn: McConnection) -> tuple[McConnection, LogBatch] | None:
    if conn.log_monitor is None:
        return None
    try:
        batch = await run_sync(conn.log_monitor.check_updates)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Minecraft log polling status=failed error_type=%s",
            audit_error_type(exc),
        )
        return None
    if batch.events or batch.dropped_events or batch.skipped_bytes:
        return conn, batch
    _commit_log_batch(conn, batch)
    return None


async def _deliver_log_batch(
    context: PluginContextProtocol,
    conn: McConnection,
    batch: LogBatch,
) -> None:
    message, event_total, granted = _batch_message(conn, batch, now=time.monotonic())
    logger.info(
        "Minecraft log delivery status=started target_type=%s event_count=%d",
        conn.target.kind,
        event_total,
    )

    def commit_cursor() -> None:
        if not _commit_log_batch(conn, batch):
            raise RuntimeError("Minecraft log cursor commit failed")

    receipt = DeliveryReceipt(
        expected_actions=1,
        commit=commit_cursor,
        rollback=lambda: _event_buckets[_server_bucket_key(conn)].refund(granted),
        # 日志 tail 不具备幂等键；结果未知时推进游标，避免重复转发可能已送达的批次。
        unknown=commit_cursor,
    )
    await _send_mc_action(context, conn, message, receipt)
    if receipt.callback_error is not None:
        _event_buckets[_server_bucket_key(conn)].refund(granted)


async def _run_scheduled(context: PluginContextProtocol) -> None:
    global _delivery_cursor
    connections = _manager.all_connections()
    if not connections:
        return

    active_bucket_keys = {_server_bucket_key(conn) for conn in connections}
    for key in tuple(_event_buckets):
        if key not in active_bucket_keys:
            _event_buckets.pop(key, None)

    start = _delivery_cursor % len(connections)
    ordered = connections[start:] + connections[:start]
    _delivery_cursor = (start + MC_MAX_ACTIONS_PER_TICK) % len(connections)
    polled = await asyncio.gather(*(_poll_connection(conn) for conn in ordered))
    deliveries = [delivery for delivery in polled if delivery is not None]
    await asyncio.gather(
        *(
            _deliver_log_batch(context, conn, batch)
            for conn, batch in deliveries[:MC_MAX_ACTIONS_PER_TICK]
        )
    )


async def scheduled(context: PluginContextProtocol) -> None:
    """并发轮询日志，按轮转顺序选择至多五个独立目标进行投递。"""

    if _schedule_lock.locked():
        logger.warning("Minecraft log schedule status=skipped reason=previous_tick_running")
        return
    async with _schedule_lock:
        await _run_scheduled(context)


__all__ = ["handle", "init", "scheduled", "shutdown"]
