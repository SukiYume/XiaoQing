"""日程详情页的纯展示逻辑。

本模块只把事件图转换为 QQ 文本，不负责查库、修改日程或分发命令。
这样详情展示可以独立测试，也避免 ``EventHandler`` 同时承担过多职责。
"""

from __future__ import annotations

from typing import Any

from ..core.types import CommandMessage
from ..models.item import EventItem
from ..services.event_graph import EventFamily
from ..utils.formatters import ItemFormatter
from ..utils.identifiers import public_id
from ..utils.time_utils import parse_remind_times
from .event_support import event_display_timezone


class EventDetailViewMixin:
    """把事件集合、单个节点和提醒摘要格式化为详情消息。"""

    def _format_event_family_detail(
        self,
        family: EventFamily,
        query_id: str,
    ) -> CommandMessage:
        """根据事件图类型选择集合详情或叶节点详情。"""
        if family.collection and family.leaf is None:
            return self._format_collection_detail(family.collection, family.children)

        if family.leaf is None:
            return {"status": "error", "message": f"❌ 找不到日程 {query_id}"}
        return self._format_leaf_detail(family)

    @classmethod
    def _format_collection_detail(
        cls,
        collection: dict[str, Any],
        children: list[EventItem],
    ) -> CommandMessage:
        """格式化日程集合及其节点摘要。"""
        kind_label = "多时间节点事件" if collection.get("kind") == "multi_node" else "重复日程"
        lines = [
            f"📋 **{collection.get('title') or '无标题'}**",
            "",
            f"🗺️ {kind_label} ({len(children)}个节点)",
        ]
        if collection.get("start_time"):
            lines.append(
                f"⏰ {cls._format_full_time_range(collection['start_time'], collection.get('end_time'), collection)}"
            )
        if collection.get("category"):
            lines.append(f"📂 分类: {collection['category']}")
        if collection.get("content"):
            lines.append(f"📄 内容: {collection['content']}")
        if collection.get("location"):
            lines.append(f"📍 {collection['location']}")
        if collection.get("notes"):
            lines.append(f"📝 {collection['notes']}")
        if collection.get("tags"):
            lines.append(f"🏷️ {', '.join(collection['tags'])}")

        lines.append("")
        for child in children:
            child_time = cls._format_full_time_range(child.start_time, child.end_time, child)
            lines.append(f"  📌 {child_time} {child.title or '无标题'} `{child.display_id}`")
        collection_display_id = public_id(collection["id"])
        lines.extend(
            [
                "",
                f"`{collection_display_id}`",
                f"💡 /pendo event edit {collection_display_id} <内容> 编辑标题/元信息",
            ]
        )
        return {"status": "success", "message": "\n".join(lines)}

    def _format_leaf_detail(self, family: EventFamily) -> CommandMessage:
        """格式化单次日程或集合中的一个节点。"""
        event = family.leaf
        if event is None:
            return {"status": "error", "message": "❌ 找不到日程"}

        collection = family.collection
        title = event.title or "无标题"
        remind_times = parse_remind_times(event.remind_times)
        lines = [f"📋 **{title}**", ""]

        if collection:
            collection_display_id = public_id(collection.get("id"))
            lines.append(f"🗓️ 所属: {collection.get('title') or '无标题'} `{collection_display_id}`")
            lines.append("📌 节点日程" if collection.get("kind") == "multi_node" else "🔄 重复实例")
        else:
            lines.append("📆 单次事件")
        lines.append(f"⏰ {self._format_full_time_range(event.start_time, event.end_time, event)}")

        self._append_event_metadata(lines, event)
        self._append_reminder_preview(lines, remind_times, event)
        self._append_sibling_summary(lines, event, family.children if collection else [])

        lines.append(f"\n`{event.display_id}`")
        lines.append(
            f"💡 /pendo event reminders {event.display_id} | "
            f"/pendo event edit {event.display_id} <内容>"
        )
        return {"status": "success", "message": "\n".join(lines)}

    @staticmethod
    def _append_event_metadata(lines: list[str], event: EventItem) -> None:
        """追加分类、内容、地点、备注和标签。"""
        if event.category:
            lines.append(f"📂 分类: {event.category}")
        if event.content:
            lines.append(f"📄 内容: {event.content}")
        if event.location:
            lines.append(f"📍 {event.location}")
        if event.notes:
            lines.append(f"📝 {event.notes}")
        if event.tags:
            lines.append(f"🏷️ {', '.join(event.tags)}")

    @staticmethod
    def _format_full_time_range(
        start_time: str | None,
        end_time: str | None,
        timezone_source: EventItem | dict[str, Any] | None = None,
    ) -> str:
        """格式化详情页时间，始终保留年份和完整日期。"""
        if not start_time:
            return "未设置时间"
        display_timezone = event_display_timezone(timezone_source or {})
        start = str(ItemFormatter.format_datetime(start_time, tz=display_timezone))
        if not end_time:
            return start
        end = ItemFormatter.format_datetime(end_time, tz=display_timezone)
        return f"{start} - {end}"

    @staticmethod
    def _append_reminder_preview(
        lines: list[str],
        remind_times: list[str],
        timezone_source: EventItem | dict[str, Any],
    ) -> None:
        """追加最多五条提醒预览，避免详情消息无限增长。"""
        lines.append("")
        if not remind_times:
            lines.append("🔔 未设置提醒")
            return

        lines.append(f"🔔 提醒 ({len(remind_times)}个):")
        display_timezone = event_display_timezone(timezone_source)
        for remind_time in remind_times[:5]:
            formatted = ItemFormatter.format_datetime(
                remind_time,
                "%m月%d日 %H:%M",
                tz=display_timezone,
            )
            lines.append(f"  ⏰ {formatted}")
        if len(remind_times) > 5:
            lines.append(f"  … 共{len(remind_times)}个提醒")

    @classmethod
    def _append_sibling_summary(
        cls,
        lines: list[str],
        event: EventItem,
        children: list[EventItem],
    ) -> None:
        """追加同集合其他节点的简要索引。"""
        siblings = [child for child in children if child.id != event.id]
        if not siblings:
            return

        lines.extend(["", "同组其他节点:"])
        for sibling in siblings:
            sibling_time = cls._format_full_time_range(
                sibling.start_time,
                sibling.end_time,
                sibling,
            )
            lines.append(f"  • {sibling_time} {sibling.title or '无标题'} `{sibling.display_id}`")
