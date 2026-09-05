"""Pendo 的消息格式化和命令元数据提取工具。"""

import re
from datetime import datetime, tzinfo
from typing import Any

UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def single_line_text(value: Any) -> str:
    """移除控制字符并把用户可见字段压缩成安全单行。"""

    text = UNSAFE_CONTROL_RE.sub("", str(value or ""))
    return " ".join(text.split())


def ledger_amount_yuan(item: Any) -> float:
    """读取账目金额（元），优先使用当前规范的整数分字段。"""

    cents = getattr(item, "amount_cents", None)
    if cents not in (None, ""):
        try:
            return int(cents) / 100
        except (TypeError, ValueError):
            pass
    try:
        return float(getattr(item, "amount", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# 优先级图标映射
PRIORITY_ICONS = {
    1: "🔴",  # 紧急
    2: "🟠",  # 高
    3: "🟡",  # 中
    4: "🟢",  # 低
    5: "⚪",  # 最低
}

# 优先级文本映射
PRIORITY_LABELS = {
    1: "🔴紧急",
    2: "🟠高",
    3: "🟡中",
    4: "🟢低",
    5: "⚪最低",
}

# 任务状态图标
STATUS_ICONS = {
    "open": "⬜",
    "done": "✅",
    "cancelled": "❌",
}

# 条目类型图标
TYPE_ICONS = {
    "event": "🗓️",
    "task": "✅",
    "note": "📝",
    "diary": "📔",
    "idea": "💡",
    "ledger": "💰",
}

# 条目类型名称
TYPE_NAMES = {
    "event": "🗓️ 日程",
    "task": "✅ 待办",
    "note": "📝 笔记",
    "idea": "💡 想法",
    "diary": "📔 日记",
    "ledger": "💰 记账",
}

TAG_NAME_PATTERN = r"[\w\u4e00-\u9fa5-]+"
TAG_TOKEN_RE     = re.compile(rf"(?<!\S)#({TAG_NAME_PATTERN})(?=\s|$)")


def extract_tags(text: str | None) -> list[str]:
    return TAG_TOKEN_RE.findall(text or "")


def is_tag_token(text: str | None) -> bool:
    return re.fullmatch(rf"#{TAG_NAME_PATTERN}", text or "") is not None


class ItemFormatter:
    """条目格式化工具类

    提供统一的格式化方法，避免各Handler中的重复代码。
    """

    @staticmethod
    def format_priority(priority: int) -> str:
        """格式化优先级

        Args:
            priority: 优先级值 (1-5)

        Returns:
            格式化后的优先级字符串（带图标）
        """
        return PRIORITY_LABELS.get(priority, PRIORITY_LABELS[3])

    @staticmethod
    def format_priority_icon(priority: int) -> str:
        """获取优先级图标

        Args:
            priority: 优先级值 (1-5)

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
        return STATUS_ICONS.get(status, "⬜")

    @staticmethod
    def format_type_icon(item_type: str) -> str:
        """获取类型图标

        Args:
            item_type: 条目类型

        Returns:
            类型图标字符串
        """
        return TYPE_ICONS.get(item_type, "📄")

    @staticmethod
    def format_datetime(
        dt_str: str,
        fmt: str = "%Y-%m-%d %H:%M",
        *,
        tz: tzinfo,
    ) -> str:
        """格式化日期时间字符串

        Args:
            dt_str: ISO格式时间字符串
            fmt: 输出格式

        Returns:
            格式化后的时间字符串，解析失败则返回原始字符串
        """
        try:
            parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(tz)
            return parsed.strftime(fmt)
        except (ValueError, TypeError):
            return dt_str

    @staticmethod
    def format_time(dt_str: str, *, tz: tzinfo) -> str:
        """格式化时间字符串

        Args:
            dt_str: ISO格式时间字符串

        Returns:
            格式化后的时间字符串 (HH:MM)
        """
        return ItemFormatter.format_datetime(dt_str, "%H:%M", tz=tz)

    @staticmethod
    def format_tags(tags: list[str]) -> str:
        """格式化标签列表

        Args:
            tags: 标签列表

        Returns:
            格式化后的标签字符串
        """
        if not tags:
            return ""
        return " ".join(f"#{tag}" for tag in tags)

    @staticmethod
    def format_time_range(
        start_time: str | None,
        end_time: str | None = None,
        *,
        tz: tzinfo,
    ) -> str:
        """格式化时间范围

        Args:
            start_time: 开始时间（ISO格式）
            end_time: 结束时间（ISO格式）

        Returns:
            格式化后的时间范围字符串
        """
        if not start_time:
            return ""

        start_str = ItemFormatter.format_time(start_time, tz=tz)

        if end_time:
            end_str = ItemFormatter.format_time(end_time, tz=tz)
            return f"{start_str} - {end_str}"

        return start_str

    @staticmethod
    def truncate_content(content: str, max_length: int = 50, suffix: str = "...") -> str:
        """截断内容

        Args:
            content: 原始内容
            max_length: 最大长度
            suffix: 截断后添加的后缀

        Returns:
            截断后的内容
        """
        if not content:
            return ""
        if len(content) <= max_length:
            return content
        return content[:max_length] + suffix


class MessageBuilder:
    """消息构建工具类

    用于构建复杂的多行消息。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add_line(self, line: str = "") -> "MessageBuilder":
        """添加一行"""
        self.lines.append(line)
        return self

    def add_item(self, icon: str, text: str, indent: int = 0) -> "MessageBuilder":
        """添加列表项"""
        prefix = "  " * indent
        self.lines.append(f"{prefix}{icon} {text}")
        return self

    def add_blank(self) -> "MessageBuilder":
        """添加空行"""
        self.lines.append("")
        return self

    def build(self) -> str:
        """构建最终消息"""
        return "\n".join(self.lines)


def paginate(
    items: list[Any], page: int = 1, page_size: int = 10, show_all: bool = False
) -> tuple[list[Any], str, bool]:
    """通用分页，返回 (display_items, page_info_str, has_more)。"""
    total = len(items)
    if show_all:
        return items, " (全部显示)", False
    start   = (page - 1) * page_size
    end     = start + page_size
    display = items[start:end]
    if page > 1:
        info = f" (第{page}页)"
    else:
        info = ""
    has_more = total > end
    return display, info, has_more
