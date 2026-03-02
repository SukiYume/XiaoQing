"""
消息格式化工具
统一处理各Handler中重复的消息格式化逻辑
"""
from typing import Any, Optional
from datetime import datetime
import json

# 优先级图标映射
PRIORITY_ICONS = {
    1: '🔴',   # 紧急
    2: '🟠',   # 高
    3: '🟡',   # 中
    4: '🟢',   # 低
}

# 优先级文本映射
PRIORITY_LABELS = {
    1: '🔴紧急',
    2: '🟠高',
    3: '🟡中',
    4: '🟢低',
}

# 任务状态图标
STATUS_ICONS = {
    'todo': '⬜',
    'in_progress': '⏳',
    'done': '✅',
    'cancelled': '❌',
}

# 条目类型图标
TYPE_ICONS = {
    'event': '🗓️',
    'task': '✅',
    'note': '📝',
    'diary': '📔',
    'idea': '💡',
}

# 条目类型名称
TYPE_NAMES = {
    'event': '🗓️ 日程',
    'task': '✅ 待办',
    'note': '📝 笔记',
    'idea': '💡 想法',
    'diary': '📔 日记'
}

class ItemFormatter:
    """条目格式化工具类

    提供统一的格式化方法，避免各Handler中的重复代码。
    """

    @staticmethod
    def format_priority(priority: int) -> str:
        """格式化优先级

        Args:
            priority: 优先级值 (1-4)

        Returns:
            格式化后的优先级字符串（带图标）
        """
        return PRIORITY_LABELS.get(priority, PRIORITY_LABELS[3])

    @staticmethod
    def format_priority_icon(priority: int) -> str:
        """获取优先级图标

        Args:
            priority: 优先级值 (1-4)

        Returns:
            优先级图标字符串
        """
        return PRIORITY_ICONS.get(priority, PRIORITY_ICONS[3])

    @staticmethod
    def format_status_icon(status: str) -> str:
        """获取状态图标

        Args:
            status: 状态字符串

        Returns:
            状态图标字符串
        """
        return STATUS_ICONS.get(status, '⬜')

    @staticmethod
    def format_type_icon(item_type: str) -> str:
        """获取类型图标

        Args:
            item_type: 条目类型

        Returns:
            类型图标字符串
        """
        return TYPE_ICONS.get(item_type, '📄')

    @staticmethod
    def format_datetime(dt_str: str, fmt: str = '%Y-%m-%d %H:%M') -> str:
        """格式化日期时间字符串

        Args:
            dt_str: ISO格式时间字符串
            fmt: 输出格式

        Returns:
            格式化后的时间字符串，解析失败则返回原始字符串
        """
        try:
            return datetime.fromisoformat(dt_str).strftime(fmt)
        except (ValueError, TypeError):
            return dt_str

    @staticmethod
    def format_date(dt_str: str) -> str:
        """格式化日期字符串

        Args:
            dt_str: ISO格式时间字符串

        Returns:
            格式化后的日期字符串 (YYYY-MM-DD)
        """
        return ItemFormatter.format_datetime(dt_str, '%Y-%m-%d')

    @staticmethod
    def format_time(dt_str: str) -> str:
        """格式化时间字符串

        Args:
            dt_str: ISO格式时间字符串

        Returns:
            格式化后的时间字符串 (HH:MM)
        """
        return ItemFormatter.format_datetime(dt_str, '%H:%M')

    @staticmethod
    def format_tags(tags: list[str]) -> str:
        """格式化标签列表

        Args:
            tags: 标签列表

        Returns:
            格式化后的标签字符串
        """
        if not tags:
            return ''
        return ' '.join(f'#{tag}' for tag in tags)

    @staticmethod
    def format_remind_times(remind_times: list[str]) -> str:
        """格式化提醒时间列表

        Args:
            remind_times: 提醒时间列表（ISO格式）

        Returns:
            格式化后的提醒时间字符串
        """
        if not remind_times:
            return '未设置提醒'

        formatted = []
        for t in remind_times:
            time_str = ItemFormatter.format_datetime(t, '%m-%d %H:%M')
            formatted.append(time_str)
        return ', '.join(formatted)

    @staticmethod
    def format_item_reference(item) -> str:
        """格式化条目引用（用于搜索结果等）

        Args:
            item: 条目数据（Item dataclass实例）

        Returns:
            格式化后的引用字符串
        """
        item_type = item.type.value if hasattr(item.type, 'value') else item.type
        icon = ItemFormatter.format_type_icon(item_type)
        title = item.title or ItemFormatter.truncate_content(item.content or '', 30)
        item_id = item.id or ''

        return f"{icon} {title} `{item_id}`"

    @staticmethod
    def format_time_range(start_time: Optional[str], end_time: Optional[str] = None) -> str:
        """格式化时间范围

        Args:
            start_time: 开始时间（ISO格式）
            end_time: 结束时间（ISO格式）

        Returns:
            格式化后的时间范围字符串
        """
        if not start_time:
            return ''

        start_str = ItemFormatter.format_time(start_time)

        if end_time:
            end_str = ItemFormatter.format_time(end_time)
            return f"{start_str} - {end_str}"

        return start_str

    @staticmethod
    def format_confirm_message(item, action: str = 'confirm') -> str:
        """格式化确认消息

        Args:
            item: 条目数据（Item dataclass实例）
            action: 操作类型

        Returns:
            确认消息字符串
        """
        lines = [f"⚠️ 请确认要{action}以下内容:", ""]

        if item.title:
            lines.append(f"📋 标题: {item.title}")

        item_type = item.type.value if hasattr(item.type, 'value') else item.type
        if item_type == 'event':
            if getattr(item, 'start_time', None):
                lines.append(f"⏰ 时间: {ItemFormatter.format_datetime(item.start_time)}")
            if getattr(item, 'location', None):
                lines.append(f"📍 地点: {item.location}")

        lines.append("")
        lines.append("回复 'yes' 确认，'no' 取消")

        return "\n".join(lines)

    @staticmethod
    def format_list_header(title: str, count: int, filter_info: str = '') -> str:
        """格式化列表头部

        Args:
            title: 列表标题
            count: 条目数量
            filter_info: 筛选条件说明

        Returns:
            格式化后的列表头部字符串
        """
        header = f"📋 **{title}** (共{count}项)"
        if filter_info:
            header += f"\n{filter_info}"
        return header

    @staticmethod
    def truncate_content(content: str, max_length: int = 50, suffix: str = '...') -> str:
        """截断内容

        Args:
            content: 原始内容
            max_length: 最大长度
            suffix: 截断后添加的后缀

        Returns:
            截断后的内容
        """
        if not content:
            return ''
        if len(content) <= max_length:
            return content
        return content[:max_length] + suffix

class MessageBuilder:
    """消息构建工具类

    用于构建复杂的多行消息。
    """

    def __init__(self):
        self.lines: list[str] = []

    def add_line(self, line: str = '') -> 'MessageBuilder':
        """添加一行"""
        self.lines.append(line)
        return self

    def add_header(self, text: str, level: int = 1) -> 'MessageBuilder':
        """添加标题"""
        prefix = '#' * level
        self.lines.append(f"{prefix} {text}")
        return self

    def add_section(self, title: str) -> 'MessageBuilder':
        """添加小节标题"""
        self.lines.append(f"\n**{title}**")
        return self

    def add_item(self, icon: str, text: str, indent: int = 0) -> 'MessageBuilder':
        """添加列表项"""
        prefix = '  ' * indent
        self.lines.append(f"{prefix}{icon} {text}")
        return self

    def add_info(self, key: str, value: str, icon: str = '') -> 'MessageBuilder':
        """添加键值对信息"""
        if icon:
            self.lines.append(f"{icon} {key}: {value}")
        else:
            self.lines.append(f"{key}: {value}")
        return self

    def add_separator(self) -> 'MessageBuilder':
        """添加分隔符"""
        self.lines.append("---")
        return self

    def add_blank(self) -> 'MessageBuilder':
        """添加空行"""
        self.lines.append("")
        return self

    def build(self) -> str:
        """构建最终消息"""
        return "\n".join(self.lines)

    def __str__(self) -> str:
        return self.build()

def format_success_message(message: str, item_id: Optional[str] = None) -> str:
    """格式化成功消息

    Args:
        message: 主要消息内容
        item_id: 条目ID（可选）

    Returns:
        格式化后的成功消息
    """
    lines = [f"✅ {message}"]
    if item_id:
        lines.append(f"\n`{item_id}`")
    return "\n".join(lines)

def format_error_message(message: str, hint: Optional[str] = None) -> str:
    """格式化错误消息

    Args:
        message: 错误消息
        hint: 提示信息（可选）

    Returns:
        格式化后的错误消息
    """
    lines = [f"❌ {message}"]
    if hint:
        lines.append(f"\n💡 {hint}")
    return "\n".join(lines)

def format_warning_message(message: str) -> str:
    """格式化警告消息

    Args:
        message: 警告消息

    Returns:
        格式化后的警告消息
    """
    return f"⚠️ {message}"

def parse_remind_times(raw: Any) -> list[str]:
    """解析提醒时间

    统一处理提醒时间的解析，避免各Handler中重复实现。

    Args:
        raw: 原始提醒时间数据（可以是列表、字符串或JSON字符串）

    Returns:
        解析后的提醒时间列表，解析失败返回空列表
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return []
