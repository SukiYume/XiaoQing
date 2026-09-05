"""把已加载插件目录整理为可注入提示词的工具能力摘要。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..planning.goal_state import load_latest_topic_and_summary


def build_tool_info_block(
    *,
    data_dir: Path,
    bot_name: str,
    chat_id: str,
    event: dict[str, Any],
    goal: str = "",
    last_reply_ts: float,
    replies_last_minute: int,
    continuous_reply_count: int,
    cooldown_left_seconds: float,
    recent_actions: Sequence[str],
) -> str:
    now      = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    group_id = event.get("group_id")
    user_id  = event.get("user_id")
    channel  = "私聊" if group_id is None else f"群聊(g{group_id})"

    lines: list[str] = []
    lines.append("可用工具信息：")
    lines.append(f"- 当前时间：{now}")
    lines.append(f"- 场景：{channel}")
    lines.append(f"- 会话：{chat_id}")
    if user_id is not None:
        lines.append(f"- 说话人：{user_id}")
    lines.append(f"- 机器人：{bot_name}")
    if goal.strip():
        lines.append(f"- 当前目标：{goal.strip()}")

    since = max(0.0, time.time() - float(last_reply_ts or 0.0)) if last_reply_ts else 0.0
    lines.append(
        f"- 频率：距上次回复{since:.0f}s，60秒内回复{int(replies_last_minute)}，连续回复{int(continuous_reply_count)}，冷却剩余{max(0.0, cooldown_left_seconds):.0f}s"
    )

    topic, summary = load_latest_topic_and_summary(data_dir, chat_id)
    topic_text = ""
    if topic and summary:
        topic_text = f"最新话题：{topic}\n摘要：{summary}"
    elif topic:
        topic_text = f"最新话题：{topic}"
    if topic_text:
        lines.append("- " + topic_text.replace("\n", "\n  "))

    if recent_actions:
        lines.append("- 最近动作：")
        for a in list(recent_actions)[-6:]:
            a = (a or "").strip()
            if a:
                lines.append(f"  - {a}")

    return "\n".join(lines).strip() + "\n"
