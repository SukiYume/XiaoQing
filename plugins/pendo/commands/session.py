"""分发并推进 Pendo 的多轮命令会话。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from ..config import PendoConfig
from ..core.runtime import get_cached_services
from ..core.types import CommandMessage, PendoContext, PendoServices, SessionData
from ..utils.error_handlers import error_result, info_result
from ..utils.session_utils import safe_create_session, safe_end_session

if TYPE_CHECKING:
    from ..services.ai_parser import AIParser


_EVENT_INFO_FIELDS: Final = (
    "start_time",
    "end_time",
    "location",
    "title",
    "content",
    "category",
    "tags",
)


def _require_services(context: PendoContext) -> PendoServices:
    """返回绑定到当前上下文的完整服务集合。"""

    services = get_cached_services(context)
    if services is None:
        raise RuntimeError("Pendo 服务尚未初始化")
    return cast(PendoServices, services)


def _merge_event_info(
    base_data: dict[str, Any],
    parsed: dict[str, Any],
    ai_parser: AIParser,
    user_id: str,
) -> dict[str, Any]:
    """只用补充消息填充缺失字段，并按需重建提醒时间。"""

    merged = dict(base_data)
    # 所有者由已校验的入口参数决定；解析来源也不是 EventItem 持久化字段。
    merged.pop("owner_id", None)
    merged.pop("parse_source", None)
    for field in _EVENT_INFO_FIELDS:
        value = parsed.get(field)
        if not merged.get(field) and value:
            merged[field] = value

    raw_reminders = parsed.get("remind_times")
    parsed_reminders = (
        [value.strip() for value in raw_reminders if isinstance(value, str) and value.strip()]
        if isinstance(raw_reminders, list)
        else []
    )
    if not merged.get("remind_times") and parsed_reminders:
        merged["remind_times"] = parsed_reminders

    start_time = merged.get("start_time")
    raw_offsets = merged.get("remind_offsets") or parsed.get("remind_offsets")
    offsets = (
        [offset.strip() for offset in raw_offsets if isinstance(offset, str) and offset.strip()]
        if isinstance(raw_offsets, list)
        else []
    )
    if not merged.get("remind_times") and isinstance(start_time, str) and offsets:
        merged["remind_times"] = ai_parser.build_remind_times_from_offsets(
            start_time,
            offsets,
            user_id=user_id,
        )
    if merged.get("remind_times"):
        merged.pop("remind_offsets", None)

    merged["type"] = "event"
    return merged


async def handle_session_message(
    user_id: str,
    text: str,
    session: SessionData,
    context: PendoContext,
) -> CommandMessage:
    """按会话类型分发消息；损坏或过期类型会被清除以便用户重试。"""

    raw_session_type = session.get("type")
    session_type = raw_session_type if isinstance(raw_session_type, str) else "<invalid>"

    if session_type == PendoConfig.SESSION_TYPE_DIARY_TEMPLATE:
        # 用户身份来自已完成作用域校验的入口，不信任会话中的身份副本。
        diary_handler = _require_services(context)["diary_handler"]
        return await diary_handler.handle_session_message(user_id, text, context, session)

    if session_type == PendoConfig.SESSION_TYPE_EVENT_CONFLICT:
        return await handle_event_conflict_session(user_id, text, session, context)
    if session_type == PendoConfig.SESSION_TYPE_EVENT_INFO:
        return await handle_event_info_session(user_id, text, session, context)

    if session_type == PendoConfig.SESSION_TYPE_TASK_ADD:
        task_handler = _require_services(context)["task_handler"]
        return await task_handler.handle_session_step(user_id, text, session, context)

    if session_type == PendoConfig.SESSION_TYPE_LEDGER_ADD:
        ledger_handler = _require_services(context)["ledger_handler"]
        return await ledger_handler.handle_session_step(user_id, text, session, context)

    await safe_end_session(context)
    return error_result(f"未知的会话类型: {session_type}")


async def handle_event_conflict_session(
    user_id: str,
    text: str,
    session: SessionData,
    context: PendoContext,
) -> CommandMessage:
    """处理冲突确认；肯定和否定回答都会结束当前会话。"""

    response = text.strip().lower()

    if response in PendoConfig.CONFIRM_POSITIVE:
        raw_data = session.get("data")
        if not isinstance(raw_data, dict):
            await safe_end_session(context)
            return error_result("日程会话状态损坏，请重新创建")
        event_handler = _require_services(context)["event_handler"]
        # 创建过程会补默认分类和提醒，复制后不污染仍在事务中的会话快照。
        parsed_data: dict[str, Any] = dict(raw_data)
        result = await event_handler.create_event(
            user_id, parsed_data, context, allow_conflict=True
        )
        await safe_end_session(context)
        return result

    if response in PendoConfig.CONFIRM_NEGATIVE:
        await safe_end_session(context)
        return info_result("已取消创建日程")

    return info_result("请输入 yes/no 或 是/否")


async def handle_event_info_session(
    user_id: str,
    text: str,
    session: SessionData,
    context: PendoContext,
) -> CommandMessage:
    """解析日程补充信息，并根据创建结果替换或结束会话。"""

    raw_base_data = session.get("data")
    if not isinstance(raw_base_data, dict):
        await safe_end_session(context)
        return error_result("日程会话状态损坏，请重新创建")

    services = _require_services(context)
    event_handler = services["event_handler"]
    ai_parser = services["ai_parser"]

    # AIParser 自身已实现规则降级；局部模式不会把“明天九点”误填成标题或正文。
    parsed = await ai_parser.parse_event_with_ai(text, user_id, partial=True)
    merged = _merge_event_info(dict(raw_base_data), parsed, ai_parser, user_id)

    result = await event_handler.create_event(user_id, merged, context)
    status = result.get("status")
    if status == "need_confirm":
        raw_conflict_data = result.get("data")
        conflict_data = dict(raw_conflict_data) if isinstance(raw_conflict_data, dict) else merged
        replaced = await safe_create_session(
            context,
            initial_data={
                "type": PendoConfig.SESSION_TYPE_EVENT_CONFLICT,
                "data": conflict_data,
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS,
        )
        if not replaced:
            raise RuntimeError("无法把日程补充会话替换为冲突确认会话")
    elif status != "need_info":
        await safe_end_session(context)

    return result
