"""在私聊深度模式下叠加思考风格，同时保持稳定人物身份。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime


def is_brain_chat_active(
    runtime: _ChatRuntime,
    is_private: bool,
) -> bool:
    """仅在配置允许的私聊中启用深度对话。"""

    return bool(is_private and runtime.cfg.brain_chat.enable_private_brain_chat)


def get_brain_chat_identity(runtime: _ChatRuntime, is_brain_chat: bool) -> str:
    """返回稳定人物底座，并在深度模式下叠加思考方式。

    深度模式只改变如何聊，不能替换角色身份；否则进入私聊后会丢掉年龄、
    兴趣和人物边界，形成两个互不一致的“小青”。
    """

    base_identity  = str(runtime.cfg.personality.identity or "").strip()
    brain_identity = runtime.cfg.brain_chat.brain_identity
    if is_brain_chat and brain_identity:
        supplement = str(brain_identity or "").strip()
        if base_identity:
            return f"{base_identity}\n深度对话方式补充：{supplement}"
        return supplement
    return base_identity


def get_brain_chat_reply_style(runtime: _ChatRuntime, is_brain_chat: bool) -> str:
    """返回当前模式的回复风格。"""

    brain_reply_style = runtime.cfg.brain_chat.brain_reply_style
    if is_brain_chat and brain_reply_style:
        return str(brain_reply_style)
    return str(runtime.cfg.personality.reply_style)


def get_brain_chat_think_level(
    runtime: _ChatRuntime,
    is_brain_chat: bool,
    *,
    history_len: int = 0,
) -> int:
    """按当前对话模式返回思考级别。"""
    if is_brain_chat:
        return int(runtime.cfg.brain_chat.brain_think_level)
    return int(runtime.cfg.planner.resolve_think_level(history_len))


def get_brain_chat_max_context(runtime: _ChatRuntime, is_brain_chat: bool) -> int:
    """按当前对话模式返回最大上下文大小。"""
    if is_brain_chat:
        return int(runtime.cfg.brain_chat.brain_max_context_size)
    return int(runtime.cfg.max_context_size)


def get_brain_chat_temperature(runtime: _ChatRuntime, is_brain_chat: bool) -> float:
    """按当前对话模式返回生成温度。"""
    if is_brain_chat:
        return float(runtime.cfg.brain_chat.brain_temperature)
    return float(runtime.cfg.temperature)


def maybe_add_mode_indicator(reply: str, runtime: _ChatRuntime) -> str:
    """按配置给非空回复添加深度模式标识。"""

    brain_chat_cfg = runtime.cfg.brain_chat
    if brain_chat_cfg.show_mode_indicator and reply:
        indicator = brain_chat_cfg.brain_mode_indicator
        if indicator:
            return f"{str(indicator)}\n{reply}"
    return reply
