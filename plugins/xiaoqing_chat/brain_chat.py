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

    # 被强制触发时，不使用深度对话模式（使用普通模式回复）
    if forced:
        return False

    return True


def get_brain_chat_identity(runtime: _ChatRuntime) -> str:
    """获取深度对话模式下的人格描述"""
    return runtime.cfg.brain_chat.brain_identity or runtime.cfg.personality.identity


def get_brain_chat_reply_style(runtime: _ChatRuntime) -> str:
    """获取深度对话模式下的回复风格"""
    return runtime.cfg.brain_chat.brain_reply_style or runtime.cfg.personality.reply_style


def get_brain_chat_think_level(runtime: _ChatRuntime) -> int:
    """获取深度对话模式下的思考级别

    使用显式 None 检查而非 falsy fallback，避免合法值 0 被误判为未设置。
    """
    level = runtime.cfg.brain_chat.brain_think_level
    return level if level is not None else runtime.cfg.planner.resolve_think_level()


def get_brain_chat_max_context(runtime: _ChatRuntime) -> int:
    """获取深度对话模式下的最大上下文大小

    使用显式 None 检查而非 falsy fallback。
    """
    size = runtime.cfg.brain_chat.brain_max_context_size
    return size if size is not None else runtime.cfg.max_context_size


def get_brain_chat_temperature(runtime: _ChatRuntime) -> float:
    """获取深度对话模式下的温度参数

    使用显式 None 检查而非 falsy fallback，避免 temperature=0.0 被误判为未设置。
    """
    temp = runtime.cfg.brain_chat.brain_temperature
    return temp if temp is not None else runtime.cfg.temperature


def maybe_add_mode_indicator(reply: str, runtime: _ChatRuntime) -> str:
    """
    如果启用了模式指示器，在回复前添加标识

    Args:
        reply: 原始回复
        runtime: 运行时配置

    Returns:
        可能带有模式标识的回复
    """
    if runtime.cfg.brain_chat.show_mode_indicator and reply:
        indicator = runtime.cfg.brain_chat.brain_mode_indicator or ""
        if indicator:
            return f"{indicator}\n{reply}"
    return reply


def get_brain_chat_config_summary(runtime: _ChatRuntime) -> dict:
    """
    获取深度对话配置摘要（用于调试）

    Returns:
        配置摘要字典
    """
    cfg = runtime.cfg.brain_chat
    return {
        "enabled": cfg.enable_private_brain_chat,
        "planner_always_on": cfg.private_planner_always_on,
        "think_level": cfg.brain_think_level,
        "max_context": cfg.brain_max_context_size,
        "temperature": cfg.brain_temperature,
        "show_indicator": cfg.show_mode_indicator,
    }
