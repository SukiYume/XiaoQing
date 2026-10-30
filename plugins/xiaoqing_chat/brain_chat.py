"""在私聊深度模式下叠加思考风格，同时保持稳定人物身份。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .persona import SCOPE_CHAT, SCOPE_DEEP

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime


def is_brain_chat_active(
    runtime: ChatRuntime,
    is_private: bool,
) -> bool:
    """仅在配置允许的私聊中启用深度对话。"""

    return bool(is_private and runtime.cfg.brain_chat.enable_private_brain_chat)


def persona_reply_style(runtime: ChatRuntime, is_brain_chat: bool, bot_name: str) -> str:
    """人设卡里聊天用的说话方式和示例；深度模式只追加〔深度对话〕一节，不换人。"""

    card  = runtime.cfg.personality.card
    parts = [card.scoped(SCOPE_CHAT, bot_name)]
    if is_brain_chat:
        parts.append(card.scoped(SCOPE_DEEP, bot_name))
    return "\n\n".join(part for part in parts if part)


def get_brain_chat_think_level(
    runtime: ChatRuntime,
    is_brain_chat: bool,
    *,
    history_len: int = 0,
) -> int:
    """按当前对话模式返回思考级别。"""
    if is_brain_chat:
        return int(runtime.cfg.brain_chat.brain_think_level)
    return int(runtime.cfg.planner.resolve_think_level(history_len))


def get_brain_chat_max_context(runtime: ChatRuntime, is_brain_chat: bool) -> int:
    """按当前对话模式返回最大上下文大小。"""
    if is_brain_chat:
        return int(runtime.cfg.brain_chat.brain_max_context_size)
    return int(runtime.cfg.max_context_size)


def maybe_add_mode_indicator(reply: str, runtime: ChatRuntime) -> str:
    """按配置给非空回复添加深度模式标识。"""

    brain_chat_cfg = runtime.cfg.brain_chat
    if brain_chat_cfg.show_mode_indicator and reply:
        indicator = brain_chat_cfg.brain_mode_indicator
        if indicator:
            return f"{str(indicator)}\n{reply}"
    return reply
