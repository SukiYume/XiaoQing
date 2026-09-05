"""CHIME/FRB 重复暴目录查询与定时增量通知。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from core.args import parse
from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.delivery import DeliveryReceipt, send_with_receipt
from core.durable_fanout import (
    PendingFanout,
    clear_pending,
    create_pending,
    default_group_targets,
    load_pending,
    mark_delivered,
)
from core.interfaces import PluginContextProtocol
from core.plugin_base import (
    Segments,
    bounded_external_text,
    build_action,
    load_json,
    segments,
    write_json,
)
from core.public_errors import public_error_message, public_error_response

# 官方公共目录重建期间，这个旧 JSON 端点可能暂时不可用；目前没有等价的实时归档接口。
CHIME_API_URL    = "https://catalog.chime-frb.ca/repeaters"
MAX_DISPLAY_FRBS = 5

_PULSE_DATE_RE = re.compile(r"\d{6}", re.ASCII)
_FRB_NAME_RE   = re.compile(r"FRB[A-Z0-9.+-]{1,60}", re.IGNORECASE | re.ASCII)
_TIMESTAMP_RE  = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?",
    re.ASCII,
)
_INVALID_VALUES      = frozenset({"", "n/a", "na", "none", "null", "unknown", "-"})
_MAX_ARGUMENT_CHARS  = 128
_MAX_FIELD_CHARS     = 128
_MAX_FRB_RECORDS     = 5_000
_MAX_HISTORY_RECORDS = 10_000
_MIN_TIMESTAMP = datetime.min.replace(tzinfo=UTC)
_HISTORY_FILENAME = "chime_history.json"
_FANOUT_FILENAME  = "chime_delivery.json"

_CHIME_BODY_LIMITS = BodyLimits(
    max_wire_bytes    = 4 * 1024 * 1024,
    max_decoded_bytes = 8 * 1024 * 1024,
)
_CHIME_JSON_LIMITS = JsonLimits(
    max_bytes        = _CHIME_BODY_LIMITS.max_decoded_bytes,
    max_depth        = 64,
    max_nodes        = 100_000,
    max_string_chars = 6 * 1024 * 1024,
)
_CHIME_CACHE_KEY         = "chime_catalog_cache"
_CHIME_CACHE_TTL_SECONDS = 5 * 60
_CATALOG_FETCH_LOCK      = asyncio.Lock()
_DELIVERY_LOCK           = asyncio.Lock()

HELP_TEXT = """
📡 CHIME FRB 重复暴监测

用法
/chime
  预览上次成功通知后的更新
/chime list
  列出最近更新的 5 个 FRB
/chime <FRB名称>
  查询指定 FRB
/chime help
  显示本帮助

定时任务每天 9:00 和 21:00 检查；只有全部目标群确认收到通知后，才会推进本地历史记录。
""".strip()


class ChimeHistoryError(ValueError):
    """本地 CHIME 历史记录不可信。"""


def _parse_timestamp(value: str) -> datetime | None:
    """把目录时间转成 UTC 排序值；无时区值按目录惯例视为 UTC。"""
    if not _TIMESTAMP_RE.fullmatch(value):
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_pulse_date(value: object) -> datetime | None:
    """解析 YYMMDD 脉冲键，同时校验格式和实际日历日期。"""
    if not isinstance(value, str) or not _PULSE_DATE_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%y%m%d")
    except ValueError:
        return None


def _extract_scalar(data: dict[str, Any], key: str) -> str | None:
    """只接收适合进入消息的有界标量，拒绝容器、控制字符和非有限数。"""
    value = data.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    text_value = bounded_external_text(
        value,
        max_chars = _MAX_FIELD_CHARS,
        max_bytes = _MAX_FIELD_CHARS * 4,
        suffix    = "",
        truncate  = False,
    )
    if not text_value or not text_value.isprintable() or text_value.casefold() in _INVALID_VALUES:
        return None
    return text_value


class FRBData:
    """一个已规范化的重复暴条目。"""

    __slots__ = (
        "dec",
        "dm",
        "latest_pulse",
        "name",
        "observed_at",
        "pulses",
        "ra",
        "snr",
        "timestamp",
    )

    def __init__(self, name: object, info: object) -> None:
        normalized_name = (
            name.strip().upper() if isinstance(name, str) and len(name) <= _MAX_FIELD_CHARS else ""
        )
        self.name = normalized_name if _FRB_NAME_RE.fullmatch(normalized_name) else ""
        record    = info if isinstance(info, dict) else {}

        # 先解析实际日期再排序，避免跨世纪时按字符串把 99 年误排在 00 年之后。
        pulse_dates: list[tuple[datetime, str]] = []
        for key in record:
            pulse_date = _parse_pulse_date(key)
            if pulse_date is not None:
                pulse_dates.append((pulse_date, key))
        self.pulses       = tuple(key for _, key in sorted(pulse_dates))
        self.latest_pulse = self.pulses[-1] if self.pulses else None
        pulse             = record.get(self.latest_pulse) if self.latest_pulse else None
        pulse_data        = pulse if isinstance(pulse, dict) else {}

        raw_timestamp    = _extract_scalar(pulse_data, "timestamp")
        self.observed_at = _parse_timestamp(raw_timestamp) if raw_timestamp else None
        self.timestamp   = raw_timestamp if self.observed_at is not None else None
        self.dm          = _extract_scalar(pulse_data, "dm")
        self.snr         = _extract_scalar(pulse_data, "snr")
        self.ra          = _extract_scalar(record, "ra")
        self.dec         = _extract_scalar(record, "dec")

    def is_valid(self) -> bool:
        """名称、最新脉冲和观测时间都可信时才允许进入后续流程。"""
        return bool(self.name and self.latest_pulse and self.observed_at)

    def format_info(self) -> str:
        """生成有界、可直接发送的 FRB 摘要。"""
        return "\n".join(
            (
                f"FRB: {self.name}",
                f"Time: {self.timestamp or 'N/A'}",
                f"DM: {self.dm or 'N/A'}",
                f"RA: {self.ra or 'N/A'}",
                f"DEC: {self.dec or 'N/A'}",
                f"SNR: {self.snr or 'N/A'}",
            )
        )


def _cached_catalog(
    runtime_state: dict[str, Any] | None,
    now: float,
) -> dict[str, Any] | None:
    """只返回结构完整且仍在有效期内的当前插件代缓存。"""

    if runtime_state is None:
        return None
    cached = runtime_state.get(_CHIME_CACHE_KEY)
    if not isinstance(cached, dict):
        return None
    data = cached.get("data")
    if not isinstance(data, dict):
        return None
    expires_at = cached.get("expires_at")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float))
        or expires_at <= now
    ):
        return None
    return data


async def fetch_chime_repeaters(
    context: PluginContextProtocol,
    *,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """通过旧版官方 JSON 端点读取目录，并合并并发的冷缓存请求。"""

    runtime_state = getattr(context, "state", None)
    state         = runtime_state if isinstance(runtime_state, dict) else None
    loop          = asyncio.get_running_loop()
    if not force_refresh and (cached := _cached_catalog(state, loop.time())) is not None:
        return cached

    # 插件默认并行执行；锁内再次检查缓存，避免冷启动时同时下载多份大型目录。
    async with _CATALOG_FETCH_LOCK:
        if not force_refresh and (cached := _cached_catalog(state, loop.time())) is not None:
            return cached
        try:
            context.logger.info("正在请求 CHIME 重复暴目录")
            response = await aiohttp_request_bounded(
                context.http_session,
                "POST",
                CHIME_API_URL,
                limits         = _CHIME_BODY_LIMITS,
                mime_policy    = JSON_MIME_POLICY,
                request_kwargs = {"json": {}, "timeout": 30},
            )
            data = parse_bounded_json(response, limits=_CHIME_JSON_LIMITS)
            if not isinstance(data, dict):
                context.logger.error("CHIME 目录响应格式错误：顶层不是对象")
                return None
            if state is not None:
                state[_CHIME_CACHE_KEY] = {
                    "data": data,
                    # 从请求成功时起计算完整 TTL，慢请求不会吞掉缓存寿命。
                    "expires_at": loop.time() + _CHIME_CACHE_TTL_SECONDS,
                }
            context.logger.info("成功获取 CHIME 数据，共 %d 个重复暴条目", len(data))
            return data
        except HttpStatusError as exc:
            context.logger.warning("CHIME 目录请求失败：HTTP %s", exc.status)
        except TimeoutError:
            context.logger.warning("CHIME 目录请求超时")
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = context.logger,
                component = "chime.fetch",
            )
        return None


def parse_frb_data(
    data: dict[str, Any],
    context: PluginContextProtocol,
) -> list[FRBData]:
    """一次性解析整个响应；规范化名称冲突时拒绝整批数据。"""
    if len(data) > _MAX_FRB_RECORDS:
        context.logger.error("CHIME 目录条目数超过安全上限")
        return []

    parsed: list[FRBData] = []
    seen_names: set[str]  = set()
    invalid_count         = 0
    for name, info in data.items():
        frb = FRBData(name, info)
        if not frb.is_valid():
            invalid_count += 1
            continue
        if frb.name in seen_names:
            context.logger.error("CHIME 目录含规范化后重名的 FRB，拒绝本批数据")
            return []
        seen_names.add(frb.name)
        parsed.append(frb)

    if invalid_count:
        context.logger.debug("CHIME 目录中有 %d 条记录未通过字段校验", invalid_count)
    return parsed


def _validate_history(value: object) -> dict[str, str]:
    """严格验证通知基线，避免损坏状态被当成空历史而触发全量重发。"""
    if not isinstance(value, dict) or len(value) > _MAX_HISTORY_RECORDS:
        raise ChimeHistoryError("CHIME history root is invalid")
    history: dict[str, str] = {}
    for name, timestamp in value.items():
        if (
            not isinstance(name, str)
            or name != name.strip().upper()
            or not _FRB_NAME_RE.fullmatch(name)
            or not isinstance(timestamp, str)
            or len(timestamp) > _MAX_FIELD_CHARS
            or _parse_timestamp(timestamp) is None
        ):
            raise ChimeHistoryError("CHIME history entry is invalid")
        history[name] = timestamp
    return history


def _history_path(context: PluginContextProtocol) -> Path:
    return Path(context.data_dir) / _HISTORY_FILENAME


def _fanout_path(context: PluginContextProtocol) -> Path:
    return Path(context.data_dir) / _FANOUT_FILENAME


def load_history(context: PluginContextProtocol) -> dict[str, str]:
    """严格加载已确认通知的最新时间；文件缺失表示尚无基线。"""
    value: object = load_json(_history_path(context), {}, raise_on_error=True)
    return _validate_history(value)


def save_history(context: PluginContextProtocol, mapping: object) -> bool:
    """原子保存通过完整校验的通知基线。"""
    try:
        history = _validate_history(mapping)
        write_json(_history_path(context), history)
        context.logger.debug("CHIME 历史记录已保存：%d 条", len(history))
        return True
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "chime.save_history",
        )
        return False


def find_updates(
    frb_list: list[FRBData],
    old_mapping: dict[str, str],
    context: PluginContextProtocol,
) -> tuple[list[FRBData], list[FRBData]]:
    """只把新增源或严格晚于基线的观测视为更新。"""
    new_repeaters: list[FRBData] = []
    new_pulses: list[FRBData]    = []
    regressed                    = 0
    for frb in frb_list:
        old_timestamp = old_mapping.get(frb.name)
        if old_timestamp is None:
            new_repeaters.append(frb)
            continue
        old_observed_at = _parse_timestamp(old_timestamp)
        if old_observed_at is None or frb.observed_at is None:
            raise ChimeHistoryError("CHIME history comparison timestamp is invalid")
        if frb.observed_at > old_observed_at:
            new_pulses.append(frb)
        elif frb.observed_at < old_observed_at:
            regressed += 1

    if new_repeaters:
        context.logger.info("CHIME 检测到 %d 个新重复暴", len(new_repeaters))
    if new_pulses:
        context.logger.info("CHIME 检测到 %d 个新脉冲", len(new_pulses))
    if regressed:
        context.logger.warning("CHIME 响应中有 %d 个时间戳早于本地基线，已忽略", regressed)
    return new_repeaters, new_pulses


def merge_history(
    old_mapping: dict[str, str],
    frb_list: list[FRBData],
) -> dict[str, str]:
    """保留响应中暂时缺失的源，并且绝不把基线回退到更早时间。"""
    merged = dict(old_mapping)
    for frb in frb_list:
        if frb.timestamp is None or frb.observed_at is None:
            raise ValueError("cannot merge an invalid FRB record")
        previous      = merged.get(frb.name)
        previous_time = _parse_timestamp(previous) if previous else None
        if previous_time is None or frb.observed_at > previous_time:
            merged[frb.name] = frb.timestamp
    return merged


def format_update_message(
    new_repeaters: list[FRBData],
    new_pulses: list[FRBData],
    *,
    is_scheduled: bool = False,
) -> str:
    """把增量结果压缩为最多各五条的通知文本。"""
    lines = ["🔔 CHIME FRB 更新通知", ""] if is_scheduled else []

    if new_repeaters:
        lines.append("🆕 新发现的重复暴")
        for frb in new_repeaters[:MAX_DISPLAY_FRBS]:
            lines.extend((frb.format_info(), ""))
        if len(new_repeaters) > MAX_DISPLAY_FRBS:
            lines.extend((f"... 还有 {len(new_repeaters) - MAX_DISPLAY_FRBS} 个", ""))

    if new_pulses:
        lines.append("📡 检测到新脉冲")
        for frb in new_pulses[:MAX_DISPLAY_FRBS]:
            lines.extend((frb.format_info(), ""))
        if len(new_pulses) > MAX_DISPLAY_FRBS:
            lines.extend((f"... 还有 {len(new_pulses) - MAX_DISPLAY_FRBS} 个", ""))

    return "\n".join(lines).rstrip()


def _sorted_frbs_by_latest_timestamp(frb_list: list[FRBData]) -> list[FRBData]:
    return sorted(
        frb_list,
        key     = lambda item: item.observed_at or _MIN_TIMESTAMP,
        reverse = True,
    )


def _parse_request(args: object) -> tuple[str, str] | None:
    """把有界命令参数分类为 help、list、frb 或默认更新预览。"""
    if not isinstance(args, str) or len(args) > _MAX_ARGUMENT_CHARS:
        return None
    parsed = parse(args)
    if parsed.options or len(parsed.tokens) > 1:
        return None

    query = parsed.first.strip()
    if query.casefold() in {"help", "帮助"}:
        return "help", ""
    if query.casefold() in {"list", "列表"}:
        return "list", ""
    normalized_query = query.upper()
    if query and _FRB_NAME_RE.fullmatch(normalized_query):
        return "frb", normalized_query
    return ("updates", "") if not query else ("unknown", "")


def _render_direct_query(
    mode: str,
    normalized_query: str,
    frb_list: list[FRBData],
) -> Segments | None:
    """渲染不依赖通知历史的列表或单源查询；更新预览返回 None。"""
    if mode == "list":
        latest = _sorted_frbs_by_latest_timestamp(frb_list)[:MAX_DISPLAY_FRBS]
        lines  = ["📡 最近更新的 FRB："]
        lines.extend(f"• {frb.name} - {frb.timestamp}" for frb in latest)
        if len(frb_list) > MAX_DISPLAY_FRBS:
            lines.append(f"\n... 共 {len(frb_list)} 个")
        return segments("\n".join(lines))
    if mode != "frb":
        return None

    frb = next((item for item in frb_list if item.name == normalized_query), None)
    if frb is None:
        return segments(f"❌ 未找到 FRB「{normalized_query}」")
    return segments(frb.format_info())


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> Segments:
    """处理列表、单源查询和增量预览；入口形参遵循统一插件契约。"""
    try:
        request = _parse_request(args)
        if request is None:
            return segments(f"参数格式错误\n{HELP_TEXT}")
        mode, normalized_query = request
        if mode == "help":
            return segments(HELP_TEXT)
        if mode == "unknown":
            return segments(f"未知命令\n{HELP_TEXT}")

        data = await fetch_chime_repeaters(context)
        if not data:
            return segments("❌ 无法获取 CHIME FRB 数据，请稍后重试")
        frb_list = parse_frb_data(data, context)
        if not frb_list:
            return segments("❌ 未能解析到有效的 FRB 数据")

        direct_response = _render_direct_query(mode, normalized_query, frb_list)
        if direct_response is not None:
            return direct_response

        old_mapping = load_history(context)
        new_repeaters, new_pulses = find_updates(frb_list, old_mapping, context)
        if new_repeaters or new_pulses:
            return segments(format_update_message(new_repeaters, new_pulses, is_scheduled=False))

        latest_frb = _sorted_frbs_by_latest_timestamp(frb_list)[0]
        return segments(
            "\n".join(
                (
                    "📊 没有新的重复暴观测，目前最新的是:",
                    latest_frb.format_info(),
                    "",
                    f"当前共追踪 {len(frb_list)} 个重复暴",
                )
            )
        )
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger    = context.logger,
            component = "chime.handle",
        )


async def _deliver_pending(
    context: PluginContextProtocol,
    pending: PendingFanout,
) -> bool:
    """逐目标投递并持久确认；确认状态落盘失败时停止后续投递。"""
    path = _fanout_path(context)
    for target in pending.pending_targets():
        action = build_action(pending.payload, None, target.target_id)
        if action is None:
            context.logger.error("CHIME 通知无法构造目标动作")
            continue

        confirm_target = partial(mark_delivered, path, pending, target)

        receipt = DeliveryReceipt(
            expected_actions = 1,
            commit           = confirm_target,
            rollback         = lambda: None,
            # 这是公告型 fanout；结果未知时采用 at-most-once，避免重复群公告。
            unknown=confirm_target,
        )
        try:
            await send_with_receipt(context.send_action, action, receipt)
        except Exception as exc:
            # 发送器异常可能携带远端正文或完整 action，只记录异常类型。
            context.logger.error("CHIME 通知发送异常：%s", type(exc).__name__)
            await receipt.record(False)
        if receipt.callback_error is not None:
            public_error_message(
                context,
                receipt.callback_error,
                logger    = context.logger,
                component = "chime.delivery_ack",
            )
            return False
        if not receipt.resolved or receipt.outcome is False:
            context.logger.warning("CHIME 通知发送失败，目标保留待重试")
            continue
        if receipt.outcome is None:
            context.logger.warning(
                "CHIME 通知投递结果未知；按 at-most-once 策略确认目标以避免重复公告"
            )

    if not pending.complete:
        return False
    if not save_history(context, pending.commit.get("history")):
        context.logger.warning("CHIME 历史提交失败，已完成的通知仍保留")
        return False
    try:
        clear_pending(path)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "chime.delivery_clear",
        )
        return False
    return True


async def scheduled_check(context: PluginContextProtocol) -> Segments:
    """串行执行定时检查，避免并发任务重复创建同一通知。"""
    async with _DELIVERY_LOCK:
        try:
            return await _scheduled_check_locked(context)
        except Exception as exc:
            public_error_message(
                context,
                exc,
                logger    = context.logger,
                component = "chime.scheduled",
            )
            return []


async def _scheduled_check_locked(context: PluginContextProtocol) -> Segments:
    context.logger.info("CHIME 定时检查开始")

    # 有未完成 outbox 时只重试投递，不重新请求目录或生成另一份通知。
    try:
        pending = load_pending(_fanout_path(context))
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "chime.load_delivery_state",
        )
        return []
    if pending is not None:
        await _deliver_pending(context, pending)
        return []

    data = await fetch_chime_repeaters(context, force_refresh=True)
    if not data:
        context.logger.warning("CHIME 定时检查未获取到数据")
        return []
    frb_list = parse_frb_data(data, context)
    if not frb_list:
        context.logger.warning("CHIME 定时检查未解析到有效数据")
        return []
    # 手动查询不与定时投递共用发送锁；目录读取是只读的，最多观察到已提交历史
    # 之前的快照，不会改变 outbox 的提交顺序。
    try:
        old_mapping = load_history(context)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "chime.load_history",
        )
        return []

    new_repeaters, new_pulses = find_updates(frb_list, old_mapping, context)
    new_mapping = merge_history(old_mapping, frb_list)
    if not new_repeaters and not new_pulses:
        context.logger.info("CHIME 定时检查没有新数据")
        return []

    context.logger.info(
        "CHIME 定时检查发现更新：新重复暴 %d 个，新脉冲 %d 个",
        len(new_repeaters),
        len(new_pulses),
    )
    targets = default_group_targets(context)
    if not targets:
        context.logger.warning("CHIME 通知没有可用目标群，历史记录保持不变")
        return []

    message = format_update_message(new_repeaters, new_pulses, is_scheduled=True)
    try:
        canonical_history = json.dumps(
            new_mapping,
            ensure_ascii = False,
            sort_keys    = True,
            separators   = (",", ":"),
        )
        event_id = f"chime:{hashlib.sha256(canonical_history.encode('utf-8')).hexdigest()}"
        pending  = create_pending(
            _fanout_path(context),
            event_id = event_id,
            payload  = list(segments(message)),
            targets  = targets,
            commit   = {"history": new_mapping},
        )
        await _deliver_pending(context, pending)
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger    = context.logger,
            component = "chime.delivery_state",
        )
    return []
