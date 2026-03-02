"""
Pendo Plugin - 个人时间与信息管理中枢
支持日程管理、待办事项、笔记、日记等功能

主要职责:
1. 插件生命周期管理（init/cleanup）
2. 命令入口路由（handle）
3. 定时任务入口（scheduled_*）
4. 辅助函数（_get_logger, _get_database等）

具体功能实现已拆分到:
- commands/ - 命令处理模块
- handlers/ - 业务处理器
- services/ - 核心服务
"""
import logging
import os
import time
from typing import Any, Callable

from core.plugin_base import build_action, segments, run_sync
from core.args import parse

from .core.router import CommandRouter
from .handlers.event import EventHandler
from .handlers.task import TaskHandler
from .handlers.note import NoteHandler
from .handlers.diary import DiaryHandler
from .handlers.search import SearchHandler
from .services.db import Database
from .services.reminder import ReminderService
from .services.exporter import ExporterService
from .services.ai_parser import AIParser
from .utils.time_utils import parse_custom_settings
from .utils.error_handlers import handle_command_errors_with_segments
from .config import PendoConfig

# 导入拆分的命令模块
from .commands import (
    handle_settings, handle_confirm, handle_snooze, handle_undo,
    check_reminders, send_daily_briefings, send_evening_briefings, check_diary_reminders,
    handle_session_message, migrate_undone_todos, cleanup_reminder_singleton
)

logger = logging.getLogger(__name__)

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    PendoConfig.from_env()
    PendoConfig.validate()

    db_path = os.path.join(os.path.dirname(__file__), 'data', PendoConfig.DB_FILENAME)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    # 初始化数据库（创建表结构）
    db = Database(db_path)
    log = _get_logger(context)
    log.info("Pendo plugin initialized, database at %s", db_path)

def cleanup(context=None) -> None:
    """插件清理函数 - 在插件卸载时调用"""
    try:
        db = _get_database(context)
        db.cleanup()
        from .utils.db_ops import cleanup_db_singleton
        cleanup_db_singleton()
        cleanup_reminder_singleton()  # L-5修复：清除 reminder service 单例
        runtime_state = _get_plugin_runtime_state(context, create=False)
        runtime_state.clear()
        log = _get_logger(context)
        log.info("Pendo plugin cleanup completed")
    except Exception as e:
        logger.exception("Error during cleanup: %s", e)

# ============================================================
# 主处理函数
# ============================================================

@handle_command_errors_with_segments
async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口
    
    使用统一的错误处理装饰器，提供一致的异常处理和日志记录。
    
    Args:
        command: 命令名称（通常是'pendo'）
        args: 命令参数
        event: 消息事件
        context: 上下文对象
        
    Returns:
        消息列表
    """
    log = _get_logger(context)
    user_id = str(event.get("user_id", ""))
    group_id = event.get("group_id")
    raw_message = event.get("raw_message", args)

    # 1. 优先检查是否存在活跃会话 (多轮对话)
    # 注意：只处理属于 pendo 的会话，忽略其他插件的会话
    if await _has_active_session(context, plugin_name="pendo"):
        return await _handle_active_session(user_id, raw_message, context, group_id)
    
    # 2. 解析并路由命令
    return await _handle_command_routing(user_id, args, context, group_id, log)

async def _has_active_session(context, plugin_name: str | None = None) -> bool:
    """检查是否存在活跃会话
    
    Args:
        context: 上下文对象
        plugin_name: 插件名称，如果提供则只检查该插件的会话
        
    Returns:
        是否存在（匹配的）活跃会话
    """
    if hasattr(context, 'get_session'):
        session = await context.get_session()
        if session is None:
            return False
        # 如果指定了 plugin_name，则只检查该插件的会话
        if plugin_name is not None:
            return session.plugin_name == plugin_name
        return True
    return False

async def _handle_active_session(user_id: str, raw_message: str, context, group_id: int | None = None) -> list[dict[str, Any]]:
    """处理活跃会话的消息
    
    重构后：使用commands.session模块
    
    Args:
        user_id: 用户ID
        raw_message: 原始消息
        context: 上下文对象
        group_id: 群组ID（可选）
        
    Returns:
        消息列表
    """
    session = await context.get_session()
    
    # 检查是否是退出命令
    if raw_message.strip() in PendoConfig.SESSION_EXIT_COMMANDS:
        if hasattr(context, 'end_session'):
            await context.end_session()
        return segments("✅ 已退出当前会话")
    
    # 将消息传递给会话处理器（使用commands模块）
    result = await handle_session_message(user_id, raw_message, session, context)
    
    if result and result.get('status') != 'error':
        return await _format_result(user_id, result, group_id, context)
    else:
        # 会话处理失败，清除会话
        if hasattr(context, 'end_session'):
            await context.end_session()
        if result and result.get('message'):
            return segments(result['message'])
        return []

async def _handle_command_routing(user_id: str, args: str, context, group_id: int | None = None, log=None) -> list[dict[str, Any]]:
    """处理命令路由
    
    Args:
        user_id: 用户ID
        args: 命令参数
        context: 上下文对象
        group_id: 群组ID（可选）
        log: 日志记录器
        
    Returns:
        消息列表
    """
    log = log or logger

    # 解析命令参数
    parsed = parse(args)

    router = _build_command_router(context, group_id)

    if not parsed:
        return segments(router.get_help_message())
    
    subcommand = parsed.first.lower()
    rest_args = parsed.rest(1)
    
    # 判断是否是公开命令（可以在群聊显示）
    # 公开命令：settings、无参数的子命令（显示帮助）
    public_subcommands = {'settings', 'help'}
    is_public = subcommand in public_subcommands or not rest_args.strip()

    # 使用 CommandRouter 路由子命令

    start_time = time.perf_counter()
    is_error = False
    try:
        result = await router.route(subcommand, user_id, rest_args, context)
        if isinstance(result, dict) and result.get('status') == 'error':
            is_error = True
    except Exception as e:
        is_error = True
        log.exception("Error routing command '%s' for user %s: %s", subcommand, user_id, e)
        result = {'status': 'error', 'message': f"处理命令时出错: {str(e)}"}
    finally:
        cmd_name = f"subcommand.{router.alias_map.get(subcommand.lower(), subcommand)}"
        await _record_metric(context, cmd_name, time.perf_counter() - start_time, is_error=is_error)

    return await _format_result(user_id, result, group_id, context, is_public=is_public)

async def scheduled(context) -> list[dict[str, Any]]:
    """定时任务入口 - 每分钟执行一次，只处理提醒
    
    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    
    tasks = [
        ('reminders', lambda: check_reminders(context)),
    ]
    
    messages = []
    for task_name, task_func in tasks:
        result = await _run_scheduled_task(context, task_name, task_func, log)
        messages.extend(result)
    
    return messages

async def scheduled_daily_briefing(context) -> list[dict[str, Any]]:
    """每日简报定时任务 - 每天8点执行
    
    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)
    
    result = await _run_scheduled_task(context, 'daily_briefings', 
                                     lambda: send_daily_briefings(context, db), log)
    return result

async def scheduled_evening_briefing(context) -> list[dict[str, Any]]:
    """晚间简报定时任务 - 每天21点执行
    
    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)
    
    result = await _run_scheduled_task(context, 'evening_briefings', 
                                     lambda: send_evening_briefings(context, db), log)
    return result

async def scheduled_diary_reminder(context) -> list[dict[str, Any]]:
    """日记提醒定时任务 - 每天21:30执行
    
    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)
    
    result = await _run_scheduled_task(context, 'diary_reminders', 
                                     lambda: check_diary_reminders(context, db), log)
    return result

async def scheduled_migrate_todos(context) -> list[dict[str, Any]]:
    """待办迁移定时任务 - 每天00:05执行
    
    重构后：直接调用commands.scheduled模块
    """
    log = _get_logger(context)
    db = _get_database(context)
    
    result = await _run_scheduled_task(context, 'migrate_todos',
                                     lambda: migrate_undone_todos(context, db), log)
    return result

async def _run_scheduled_task(
    context,
    task_name: str,
    task_func: Callable[[], Any],
    log: logging.Logger,
) -> list[dict[str, Any]]:
    """运行单个定时任务并记录指标
    
    统一的任务执行模式，包含：
    - 性能监控
    - 异常处理
    - 指标记录
    
    Args:
        context: 上下文对象
        task_name: 任务名称
        task_func: 任务函数
        log: 日志记录器
        
    Returns:
        任务产生的消息列表
    """
    start = time.perf_counter()
    try:
        result = await task_func()
        await _record_metric(context, f"scheduled.{task_name}", time.perf_counter() - start)
        return result if result else []
    except Exception as e:
        await _record_metric(context, f"scheduled.{task_name}", time.perf_counter() - start, is_error=True)
        log.exception("Scheduled task '%s' failed: %s", task_name, e)
        return []

async def _format_result(user_id: str, result: Any, group_id: int | None = None, context=None, is_public: bool = False) -> list[dict[str, Any]]:
    """格式化返回结果
    
    Args:
        user_id: 用户ID
        result: 命令执行结果
        group_id: 群组ID
        context: 上下文对象
        is_public: 是否是公开内容（帮助信息、settings等），公开内容不受隐私模式影响
    """
    if isinstance(result, dict):
        message = result.get('message', '')
    else:
        message = str(result)
        
    if not group_id:
        return segments(message)
    
    # 公开内容直接在群聊显示（帮助信息、settings等）
    if is_public:
        return segments(message)
    
    # 检查隐私模式
    privacy_mode = await _get_user_privacy_mode(user_id, context)
    
    # 隐私模式开启时，隐私内容发私聊
    if privacy_mode:
        group_message = "✅ 已发送私聊 (保护隐私)"
        if context is not None and hasattr(context, "send_action"):
            try:
                action = build_action(segments(message), int(user_id), None)
                if action:
                    await context.send_action(action)
            except Exception:
                logger.exception("Failed to send private message for group reply")
                return segments(message)
        return segments(group_message)
    else:
        return segments(message)

async def _get_user_privacy_mode(user_id: str, context) -> bool:
    """获取用户的隐私模式设置
    
    Args:
        user_id: 用户ID
        context: 上下文对象
        
    Returns:
        是否开启隐私模式（默认开启）
    """
    try:
        if context is not None:
            custom_settings = await _get_user_custom_settings(user_id, context)
            return custom_settings.get('privacy_mode', PendoConfig.MESSAGE_PRIVACY_MODE_DEFAULT)
    except Exception:
        return PendoConfig.MESSAGE_PRIVACY_MODE_DEFAULT
    return PendoConfig.MESSAGE_PRIVACY_MODE_DEFAULT

async def _get_user_custom_settings(user_id: str, context) -> dict[str, Any]:
    """获取用户自定义设置
    
    统一的设置获取入口，避免重复代码

    Args:
        user_id: 用户ID
        context: 上下文对象
        
    Returns:
        用户自定义设置字典
    """
    db = _get_database(context)
    settings = await run_sync(db.settings.get_user_settings, user_id)
    return parse_custom_settings(settings)

# ============================================================
# 命令处理
# ============================================================

def _build_command_router(context, group_id: int | None = None) -> CommandRouter:
    """构建命令路由器

    注意：不再按 group_id 做全局缓存，避免闭包捕获过期上下文。
    """
    
    services = _get_services(context)
    db = services['db']
    reminder_service = services['reminder_service']
    exporter = services['exporter']
    event_handler = services['event_handler']
    task_handler = services['task_handler']
    note_handler = services['note_handler']
    diary_handler = services['diary_handler']
    search_handler = services['search_handler']

    async def _export_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        return await run_sync(exporter.export_markdown, user_id, args, {})

    async def _import_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        return await run_sync(exporter.import_markdown, user_id, args, {})

    async def _settings_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        message = await handle_settings(user_id, args, db)
        return {'status': 'success', 'message': message}

    async def _confirm_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        # M-7修复：handle_confirm 现在返回 dict，直接透传，无需字符串前缀推断状态
        return await handle_confirm(user_id, args, reminder_service, db)

    async def _snooze_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        message = await handle_snooze(user_id, args, reminder_service)
        status = 'error' if message.startswith('❌') or '失败' in message else 'success'
        return {'status': status, 'message': message}

    async def _undo_cmd(user_id: str, args: str, ctx: Any) -> dict[str, Any]:
        message = await handle_undo(user_id, args, db)
        return {'status': 'success', 'message': message}

    def _help_or_exec(handler_method, help_key):
        """Helper to return help if args is empty, otherwise execute handler"""
        async def _wrapper(user_id, args, ctx):
            if not args or not args.strip():
                return {'status': 'success', 'message': _show_help(help_key)}
            return await handler_method(user_id, args, ctx, group_id=group_id)
        return _wrapper

    handlers = {
        'event': _help_or_exec(event_handler.handle, 'event'),
        'todo': _help_or_exec(task_handler.handle, 'todo'),
        'task': _help_or_exec(task_handler.handle, 'todo'),
        'note': _help_or_exec(note_handler.handle, 'note'),
        'diary': _help_or_exec(diary_handler.handle, 'diary'),
        'search': search_handler.search,
        'export': _export_cmd,
        'import': _import_cmd,
        'settings': _settings_cmd,
        'confirm': _confirm_cmd,
        'snooze': _snooze_cmd,
        'undo': _undo_cmd,
    }

    router = CommandRouter(handlers, help_provider=_show_help)
    return router

# ============================================================
# 帮助信息定义
# ============================================================

HELP_MAP = {
    "header": "🗓️ **Pendo - 个人时间与信息管理中枢**",
    "quick": [
        "**快速记录:**",
        "• /pendo event add <内容> - 添加日程(AI解析时间/地点/提醒)",
        "• /pendo todo add <内容> - 添加待办",
        "• /pendo note add <内容> - 记录笔记"
    ],
    "event": [
        "**日程管理 (Event):**",
        "• /pendo event add <内容> - 添加日程 (AI解析)",
        "  - 例: 3月8日下午两点，国自然截止，提前一周和一天提醒",
        "  - 例: 每月18号上午十点，公积金提取，重复7个月",
        "• /pendo event view <id> - 查看日程详情(里程碑/备注/提醒)",
        "• /pendo event list [范围] - 查看日程",
        "  - 范围: today, tomorrow, week, YYYY-MM, last7d, start..end",
        "• /pendo event delete <id> - 删除日程 (parentID删全部)",
        "• /pendo event edit <id> <内容> - 编辑日程",
        "• /pendo event reminders [id|范围] - 查看提醒",
        "• /pendo event reminders set <id> <描述> - 修改提醒",
        "  - 例: /pendo event reminders set abc12345 提前1天和2小时提醒"
    ],
    "todo": [
        "**待办事项 (Todo):**",
        "• /pendo todo add <内容> [cat:分类] [p:1-4] - 添加待办",
        "  - 默认添加到当天分类(cat:2026-02-02)",
        "  - 晚上8点后自动归为第二天",
        "  - p:1(紧急) p:2(高) p:3(中) p:4(低)",
        "• /pendo todo list [分类] [done/undone] [all|page:n] - 查看待办",
        "  - /pendo todo list today - 今日待办",
        "  - /pendo todo list 工作 done - 工作分类已完成",
        "  - /pendo todo list done all - 所有分类已完成(全部)",
        "  - /pendo todo list 工作 page:2 - 工作分类第2页",
        "• /pendo todo done <id> - 完成待办",
        "• /pendo todo undone <id> - 重开待办",
        "• /pendo todo delete <id|cat:分类> - 删除待办",
        "• /pendo todo edit <id> <内容> - 编辑待办"
    ],
    "note": [
        "**笔记 (Note):**",
        "• /pendo note add <内容> [cat:分类] [#标签] - 记录笔记",
        "  - 例: /pendo note add 直接折叠找脉冲星 cat:工作 #文章",
        "• /pendo note list [分类名|cat:分类] [#标签] [all|page:n] - 查看笔记",
        "  - /pendo note list - 显示所有分类概览",
        "  - /pendo note list 工作 - 查看\"工作\"分类(直接用分类名)",
        "  - /pendo note list 工作 all - 显示\"工作\"分类全部笔记",
        "  - /pendo note list 工作 page:2 - 显示\"工作\"分类第2页",
        "• /pendo note view <id> - 查看笔记详情",
        "• /pendo note edit <id> <新内容> [cat:分类] [#标签] - 编辑笔记",
        "• /pendo note delete <id|cat:分类> - 删除笔记"
    ],
    "diary": [
        "**日记 (Diary):**",
        "• /pendo diary add [日期] <内容> [weather:xxx] [location:xxx]",
        "  - 无日期则写今天的日记",
        "  - 同一天再写会自动追加到已有日记",
        "  - 自动分析情绪(开心/悲伤/平静/兴奋/愤怒)",
        "• /pendo diary list [范围] - 查看日记列表",
        "  - 范围: today, week, YYYY-MM, last7d, start..end",
        "• /pendo diary view <日期> - 查看日记详情",
        "• /pendo diary template - 查看所有模板",
        "• /pendo diary <模板ID> - 使用模板写日记(多轮引导)",
        "• /pendo diary delete <日期> - 删除日记"
    ],
    "search": [
        "**搜索 (Search):**",
        "• /pendo search <关键词> - 全文搜索",
        "• /pendo search <关键词> type=event/task/note/diary",
        "• /pendo search <关键词> range=last7d/2026-01"
    ],
    "reminder": [
        "**提醒操作:**",
        "• /pendo confirm <id> - 确认提醒",
        "• /pendo snooze <id> <时间> - 延后提醒",
        "  - 时间格式: 10m, 1h, 19:00"
    ],
    "import": [
        "**导入导出:**",
        "• /pendo export md [range] [type] - 导出Markdown",
        "• /pendo import md - 导入Markdown",
        "• /pendo import md preview - 预览导入"
    ],
    "settings": [
        "**设置 (Settings):**",
        "• /pendo settings view - 查看当前设置",
        "• /pendo settings reminder on/off - 开关提醒",
        "• /pendo settings timezone <时区> - 设置时区",
        "• /pendo settings quiet_hours <开始>-<结束> - 静默时段",
        "• /pendo settings daily_report <时间> - 每日简报时间",
        "• /pendo settings diary_remind <时间> - 日记提醒时间",
        "• /pendo settings privacy on/off - 开关隐私模式"
    ],
    "common": [
        "**其他操作:**",
        "• /pendo undo [分钟] - 撤销删除或编辑 (默认5分钟内)"
    ]
}

def _show_help(subcommand: str = "") -> str:
    """显示帮助信息
    
    Args:
        subcommand: 子命令名称，如果提供则显示特定部分的帮助
    """
    subcommand = subcommand.lower().strip() if subcommand else ""
    
    # 别名映射
    aliases = {
        'task': 'todo',
        'calendar': 'event',
        'idea': 'note',
        'journal': 'diary',
        'config': 'settings'
    }
    target_key = aliases.get(subcommand, subcommand)

    # 如果请求特定部分的帮助
    if target_key in HELP_MAP:
        parts: list[str] = [str(HELP_MAP["header"]), ""]
        section = HELP_MAP[target_key]
        if isinstance(section, list):
            parts.extend(section)
        return "\n".join(parts)
    
    # 否则显示完整帮助
    all_parts: list[str] = [str(HELP_MAP["header"]), ""]
    
    # 定义完整帮助的显示顺序
    sections = ["quick", "event", "todo", "note", "diary", "search", "reminder", "common", "import", "settings"]
    
    for key in sections:
        section = HELP_MAP[key]
        if isinstance(section, list):
            all_parts.extend(section)
        all_parts.append("") # 空行分隔
    
    all_parts.append("输入 /pendo <子命令> 可查看对应模块帮助")
    return "\n".join(all_parts)

def _get_logger(context) -> logging.Logger:
    if context is not None and hasattr(context, "logger"):
        return context.logger
    return logger

async def _record_metric(context, name: str, duration: float, is_error: bool = False) -> None:
    if context is not None and getattr(context, "metrics", None):
        await context.metrics.record_plugin_execution("pendo", name, duration, is_error=is_error)

def _get_database(context) -> Database:
    """获取数据库实例（使用共享函数）"""
    from .utils.db_ops import get_database
    return get_database(context)

def _get_plugin_runtime_state(context, *, create: bool = True) -> dict[str, Any]:
    """获取 pendo 运行时状态容器（优先 context.state）。"""
    if context is not None and hasattr(context, 'state') and isinstance(context.state, dict):
        runtime_state = context.state.get('pendo_runtime')
        if isinstance(runtime_state, dict):
            return runtime_state
        if create:
            context.state['pendo_runtime'] = {}
            return context.state['pendo_runtime']
    return {}

def _get_services(context) -> dict[str, Any]:
    """获取共享服务实例（绑定到 PluginContext.state）。"""
    runtime_state = _get_plugin_runtime_state(context)
    services = runtime_state.get('services')
    if services is not None:
        return services

    db = _get_database(context)
    ai_parser = AIParser(context)
    reminder_service = ReminderService(db)
    exporter = ExporterService(db)

    # Event需要AI解析，Task/Note/Diary不需要
    event_handler = EventHandler(db, ai_parser, reminder_service)
    task_handler = TaskHandler(db)
    note_handler = NoteHandler(db)
    diary_handler = DiaryHandler(db)
    search_handler = SearchHandler(db)

    services = {
        'db': db,
        'ai_parser': ai_parser,
        'reminder_service': reminder_service,
        'exporter': exporter,
        'event_handler': event_handler,
        'task_handler': task_handler,
        'note_handler': note_handler,
        'diary_handler': diary_handler,
        'search_handler': search_handler,
    }

    runtime_state['services'] = services

    return services
