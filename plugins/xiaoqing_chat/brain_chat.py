"""
深度对话 (Brain Chat) 模块

当启用深度对话模式时，小青会表现出更强的思考能力和洞察力。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime

from .config.config import BrainChatConfig


def is_brain_chat_active(
    runtime: _ChatRuntime,
    is_private: bool,
    forced: bool = False,
) -> bool:
    """
    检查是否处于深度对话模式

    Args:
        runtime: 运行时配置
        is_private: 是否为私聊
        forced: 是否被强制触发（@或bot名称）

    Returns:
        True 如果处于深度对话模式
    """
    # 深度对话模式仅在私聊中启用，且需要配置开启
    if not is_private:
        return False

    if not runtime.cfg.brain_chat.enable_private_brain_chat:
        return False

    return True


def get_brain_chat_identity(runtime: _ChatRuntime, is_brain_chat: bool) -> str:
    """获取对话模式下的人格描述"""
    brain_chat_cfg = getattr(runtime.cfg, "brain_chat", None)
    personality_cfg = getattr(runtime.cfg, "personality", None)
    brain_identity = getattr(brain_chat_cfg, "brain_identity", "")
    if is_brain_chat and brain_identity:
        return brain_identity
    return getattr(personality_cfg, "identity", "")


def get_brain_chat_reply_style(runtime: _ChatRuntime, is_brain_chat: bool) -> str:
    """获取对话模式下的回复风格"""
    brain_chat_cfg = getattr(runtime.cfg, "brain_chat", None)
    personality_cfg = getattr(runtime.cfg, "personality", None)
    brain_reply_style = getattr(brain_chat_cfg, "brain_reply_style", "")
    if is_brain_chat and brain_reply_style:
        return brain_reply_style
    return getattr(personality_cfg, "reply_style", "")


def get_brain_chat_think_level(
    runtime: _ChatRuntime,
    is_brain_chat: bool,
    *,
    history_len: int = 0,
) -> int:
    """获取对话模式下的思考级别
    使用显式 None 检查而非 falsy fallback，避免合法值 0 被误判为未设置。
    """
    brain_chat_cfg = getattr(runtime.cfg, "brain_chat", None)
    brain_think_level = getattr(brain_chat_cfg, "brain_think_level", None)
    if is_brain_chat and brain_think_level is not None:
        return brain_think_level
    planner_cfg = getattr(runtime.cfg, "planner", None)
    resolve_think_level = getattr(planner_cfg, "resolve_think_level", None)
    if callable(resolve_think_level):
        return int(resolve_think_level(history_len))
    return 0


def get_brain_chat_max_context(runtime: _ChatRuntime, is_brain_chat: bool) -> int:
    """获取对话模式下的最大上下文大小
    使用显式 None 检查而非 falsy fallback。
    """
    brain_chat_cfg = getattr(runtime.cfg, "brain_chat", None)
    brain_max_context_size = getattr(brain_chat_cfg, "brain_max_context_size", None)
    if is_brain_chat and brain_max_context_size is not None:
        return brain_max_context_size
    return int(getattr(runtime.cfg, "max_context_size", 30))


def get_brain_chat_temperature(runtime: _ChatRuntime, is_brain_chat: bool) -> float:
    """获取对话模式下的温度参数
    使用显式 None 检查而非 falsy fallback，避免 temperature=0.0 被误判为未设置。
    """
    brain_chat_cfg = getattr(runtime.cfg, "brain_chat", None)
    brain_temperature = getattr(brain_chat_cfg, "brain_temperature", None)
    if is_brain_chat and brain_temperature is not None:
        return float(brain_temperature)
    return float(getattr(runtime.cfg, "temperature", 0.8))


def maybe_add_mode_indicator(reply: str, runtime: _ChatRuntime) -> str:
    """
    如果启用了模式指示器，在回复前添加标识

    Args:
        reply: 原始回复
        runtime: 运行时配置

    Returns:
        可能带有模式标识的回复
    """
    brain_chat_cfg = getattr(runtime.cfg, "brain_chat", None)
    if getattr(brain_chat_cfg, "show_mode_indicator", False) and reply:
        indicator = getattr(brain_chat_cfg, "brain_mode_indicator", "") or ""
        if indicator:
            return f"{indicator}\n{reply}"
    return reply


def get_brain_chat_config_summary(runtime: _ChatRuntime) -> dict[str, object]:
    """
    获取深度对话配置摘要（用于调试）

    Returns:
        配置摘要字典
    """
    cfg = getattr(runtime.cfg, "brain_chat", None)
    return {
        "enabled": getattr(cfg, "enable_private_brain_chat", False),
        "planner_always_on": getattr(cfg, "private_planner_always_on", True),
        "think_level": getattr(cfg, "brain_think_level", BrainChatConfig().brain_think_level),
        "max_context": getattr(
            cfg, "brain_max_context_size", BrainChatConfig().brain_max_context_size
        ),
        "temperature": getattr(cfg, "brain_temperature", BrainChatConfig().brain_temperature),
        "show_indicator": getattr(cfg, "show_mode_indicator", False),
    }
