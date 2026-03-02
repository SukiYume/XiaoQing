"""
会话处理模块
处理多轮对话会话
"""
import logging
from typing import Any
from ..config import PendoConfig
from ..services.db import Database
from ..services.ai_parser import AIParser
from ..services.reminder import ReminderService
from ..handlers.diary import DiaryHandler
from ..handlers.event import EventHandler
from ..utils.db_ops import get_database

def _get_cached_services(context) -> dict[str, Any]:
    """获取缓存的服务实例"""
    if context is not None and hasattr(context, 'pendo_services'):
        return context.pendo_services
    from ..main import _get_services
    return _get_services(context)

logger = logging.getLogger(__name__)

async def handle_session_message(user_id: str, text: str, session: dict, context) -> dict[str, Any]:
    """处理会话消息分发
    
    根据会话类型分发到不同的处理器
    
    Args:
        user_id: 用户ID
        text: 用户输入文本
        session: 会话信息
        context: 上下文对象
        
    Returns:
        处理结果字典
    """
    session_type = session.get('type')
    
    # 使用字典映射处理不同会话类型
    if session_type == PendoConfig.SESSION_TYPE_DIARY_TEMPLATE:
        return await handle_diary_template_session(user_id, text, session, context)
    
    if session_type == PendoConfig.SESSION_TYPE_EVENT_CONFLICT:
        return await handle_event_conflict_session(user_id, text, session, context)
    if session_type == PendoConfig.SESSION_TYPE_EVENT_INFO:
        return await handle_event_info_session(user_id, text, session, context)
        
    return {'status': 'error', 'message': f'未知的会话类型: {session_type}'}

async def handle_diary_template_session(user_id: str, text: str, session: dict, context) -> dict[str, Any]:
    """处理日记模板会话
    
    用户按照模板逐步填写日记内容
    
    Args:
        user_id: 用户ID
        text: 用户输入文本
        session: 会话信息（包含模板信息和进度）
        context: 上下文对象
        
    Returns:
        处理结果字典
    """
    services = _get_cached_services(context)
    diary_handler = services['diary_handler']
    return await diary_handler.handle_session_message(text, context, session)

async def handle_event_conflict_session(user_id: str, text: str, session: dict, context) -> dict[str, Any]:
    """处理日程冲突会话
    
    用户确认是否创建存在时间冲突的日程
    
    Args:
        user_id: 用户ID
        text: 用户输入（是/否）
        session: 会话信息（包含待创建的日程数据）
        context: 上下文对象
        
    Returns:
        处理结果字典
    """
    response = text.strip().lower()
    
    # 用户确认创建
    if response in PendoConfig.CONFIRM_POSITIVE:
        services = _get_cached_services(context)
        event_handler = services['event_handler']
        parsed_data = session.get('data', {})
        result = await event_handler.create_event(user_id, parsed_data, context, allow_conflict=True)
        if hasattr(context, 'end_session'):
            await context.end_session()
        return result
    
    # 用户取消创建
    if response in PendoConfig.CONFIRM_NEGATIVE:
        if hasattr(context, 'end_session'):
            await context.end_session()
        return {'status': 'info', 'message': '已取消创建日程'}
    
    # 输入无效，提示重新输入
    return {'status': 'info', 'message': '请输入 yes/no 或 是/否'}

async def handle_event_info_session(user_id: str, text: str, session: dict, context) -> dict[str, Any]:
    """处理日程补充信息会话

    用户补充缺失字段（如开始时间）后继续创建日程
    """
    services = _get_cached_services(context)
    event_handler = services['event_handler']
    ai_parser = services['ai_parser']

    base_data = session.get('data', {}) if isinstance(session, dict) else {}

    # 解析补充内容
    parsed = None
    try:
        parsed = await ai_parser.parse_natural_language_with_ai(text, user_id)
    except Exception:
        parsed = ai_parser.parse_natural_language(text, user_id)

    # 只填补缺失字段，避免覆盖已有内容
    merged = dict(base_data)
    if parsed:
        for key in ['start_time', 'end_time', 'location', 'title', 'content', 'category', 'tags']:
            if not merged.get(key) and parsed.get(key):
                merged[key] = parsed.get(key)

        if (not merged.get('remind_times')) and parsed.get('remind_times'):
            merged['remind_times'] = parsed.get('remind_times')
        if (not merged.get('remind_times')) and parsed.get('remind_offsets') and merged.get('start_time'):
            merged['remind_times'] = ai_parser.build_remind_times_from_offsets(
                merged['start_time'], parsed.get('remind_offsets')
            )

    # 强制为event
    merged['type'] = 'event'

    result = await event_handler.create_event(user_id, merged, context)

    if result.get('status') == 'need_confirm' and hasattr(context, 'create_session'):
        await context.create_session(
            initial_data={
                "type": PendoConfig.SESSION_TYPE_EVENT_CONFLICT,
                "owner_id": user_id,
                "data": result.get('data', merged)
            },
            timeout=PendoConfig.SESSION_TIMEOUT_SECONDS
        )

    if result.get('status') != 'need_info':
        if hasattr(context, 'end_session'):
            await context.end_session()

    return result
