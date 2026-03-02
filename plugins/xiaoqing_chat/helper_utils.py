from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.plugin_base import Context

import asyncio as _asyncio

from .config.config import XiaoQingChatConfig, load_xiaoqing_chat_config
from .runtime_state import _ChatRuntime
from .constants import FIND_BY_LOCAL_ID_LIMIT
from .runtime_state import get_state as _state

# Lock to protect config hot-reload atomicity (issue 2.3)
_config_reload_lock = _asyncio.Lock()


def _get_lock(chat_id: str):
    """
    Get the asyncio lock for a specific chat.

    Args:
        chat_id: The chat/group identifier

    Returns:
        An asyncio.Lock for synchronizing access to this chat's state.
    """
    return _state().get_lock(chat_id)


def _chat_id(event: dict[str, Any]) -> str:
    """
    Extract a standardized chat ID from a OneBot event.

    For group chats, returns "g{group_id}". For private chats,
    returns "u{user_id}".

    Args:
        event: The OneBot event dictionary

    Returns:
        A standardized chat identifier string.
    """
    group_id = event.get("group_id")
    user_id = event.get("user_id")
    if group_id:
        return f"g{group_id}"
    return f"u{user_id}"


def _get_bot_name(context: Context) -> str:
    """
    Get the bot's configured name.

    Args:
        context: The plugin context

    Returns:
        The bot's name from config, or "小青" as default.
    """
    return (context.config or {}).get("bot_name", "") or "小青"


def _extract_sender_name(event: dict[str, Any]) -> str:
    """
    Extract the sender's display name from a OneBot event.

    Tries card, nickname, and name fields in that order.

    Args:
        event: The OneBot event dictionary

    Returns:
        The sender's display name, or "用户{user_id}" as fallback.
    """
    sender = event.get("sender") or {}
    for key in ("card", "nickname", "name"):
        v = sender.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    user_id = event.get("user_id")
    return f"用户{user_id}" if user_id else "用户"


def _is_private(event: dict[str, Any]) -> bool:
    """
    Check if an event is from a private chat.

    Args:
        event: The OneBot event dictionary

    Returns:
        True if this is a private chat event, False otherwise.
    """
    return event.get("group_id") is None


def _is_at_me(event: dict[str, Any]) -> bool:
    """
    Check if the bot was @mentioned in this message.

    Args:
        event: The OneBot event dictionary

    Returns:
        True if the bot was mentioned, False otherwise.
    """
    self_id = str(event.get("self_id", "") or "")
    if not self_id:
        return False
    message = event.get("message")
    if isinstance(message, list):
        for seg in message:
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = seg.get("data", {}).get("qq")
                if qq is not None and str(qq) == self_id:
                    return True
    raw = event.get("raw_message")
    if isinstance(raw, str) and f"[CQ:at,qq={self_id}]" in raw:
        return True
    return False


def _has_bot_name(event: dict[str, Any], bot_name: str) -> bool:
    """
    Check if the bot's name appears in the message text.

    Args:
        event: The OneBot event dictionary
        bot_name: The bot's name to check for

    Returns:
        True if the bot's name appears in the message (case-insensitive).
    """
    raw = event.get("raw_message")
    if not isinstance(raw, str) or not bot_name:
        return False
    return bot_name.lower() in raw.lower()


def _load_runtime(context: Context) -> _ChatRuntime:
    """
    Load or retrieve the cached runtime configuration for the plugin.

    Uses config/xiaoqing_config.json as the primary config path for mtime
    monitoring, consistent with the actual load priority (issue: config
    hot-reload path mismatch).

    Args:
        context: The plugin context

    Returns:
        The cached or newly loaded _ChatRuntime instance.
    """
    plugin_dir: Path = context.plugin_dir
    config_key = str(plugin_dir)
    # Fix: Monitor the same path that load_xiaoqing_chat_config actually reads
    # (config/ subdirectory takes priority over plugin root)
    config_path_sub = plugin_dir / "config" / "xiaoqing_config.json"
    config_path_root = plugin_dir / "xiaoqing_config.json"
    if config_path_sub.exists():
        config_path = config_path_sub
    elif config_path_root.exists():
        config_path = config_path_root
    else:
        config_path = config_path_sub  # fallback to expected path
    mtime = config_path.stat().st_mtime_ns if config_path.exists() else -1

    state = _state()
    cached = state.get_runtime(config_key)
    if cached is not None and state.get_runtime_mtime(config_key) == mtime:
        return cached

    cfg = load_xiaoqing_chat_config(context_config=context.config, plugin_dir=plugin_dir)
    compiled = []
    for pattern in cfg.ban_regex:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            continue

    runtime = _ChatRuntime(cfg=cfg, compiled_ban_regex=compiled)
    if cfg.knowledge.enable_knowledge and cfg.knowledge.files:
        state.memory_db.bind(context.data_dir)
        from .memory.knowledge_base import ensure_knowledge_index

        ensure_knowledge_index(
            memory_db=state.memory_db,
            data_dir=context.data_dir,
            plugin_dir=plugin_dir,
            files=cfg.knowledge.files,
        )
    state.set_runtime(config_key, runtime, mtime)
    return runtime


def _get_llm_secrets(context: Context) -> dict[str, Any]:
    """Resolve LLM provider config into a flat dict.

    The ``xiaoqing_chat`` secrets block now uses a provider-based layout::

        {
            "default": "deepseek",
            "providers": {
                "deepseek": {"api_base": "...", "api_key": "...", "model": "...", "endpoint_path": "...", "proxy": ""},
                "glm":     {"api_base": "...", ...}
            }
        }

    Resolution order:
    1. If ``state.active_provider`` is set (via ``/xc 模型 <name>``), use that provider.
    2. Otherwise use the ``default`` provider name.
    3. Flatten the provider dict so downstream code can do ``secrets.get("api_base")`` etc.

    Returns:
        Flat dict with keys: api_base, api_key, model, endpoint_path, proxy,
        plus ``_provider_name``, ``_providers`` for introspection.
    """
    from .runtime_state import get_state as _state

    raw: dict[str, Any] = (context.secrets or {}).get("plugins", {}).get("xiaoqing_chat", {}) or {}
    providers: dict[str, Any] = raw.get("providers") or {}
    default_name: str = raw.get("default", "") or ""

    state = _state()
    active = state.active_provider or default_name

    provider: dict[str, Any] = {}
    if active and active in providers:
        provider = providers[active]
    elif providers:
        # Fallback: pick the first provider if name doesn't match
        active = next(iter(providers))
        provider = providers[active]

    # Build flat dict for downstream consumption
    result: dict[str, Any] = {
        "api_base": provider.get("api_base", ""),
        "api_key": provider.get("api_key", ""),
        "model": provider.get("model", ""),
        "endpoint_path": provider.get("endpoint_path", "/v1/chat/completions"),
        "proxy": provider.get("proxy", ""),
        # Metadata for /xc 模型 display
        "_provider_name": active,
        "_providers": providers,
        "_default": default_name,
    }
    return result


def _should_ignore_text(text: str, runtime: _ChatRuntime) -> bool:
    """
    Check if text should be ignored based on ban words/regex patterns.

    Args:
        text: The text to check
        runtime: The chat runtime configuration

    Returns:
        True if the text matches any ban pattern, False otherwise.
    """
    s = text.strip()
    if not s:
        return True
    for w in runtime.cfg.ban_words:
        if w and w in s:
            return True
    for r in runtime.compiled_ban_regex:
        if r.search(s):
            return True
    return False

def _parse_local_id_num(local_id: str) -> int:
    if not local_id:
        return 0
    m = re.match(r"^m(\d+)$", local_id)
    if m:
        return int(m.group(1))
    return 0

def _next_local_id(chat_id: str) -> str:
    n = _state().get_next_local_id(chat_id)
    _state().set_next_local_id(chat_id, n + 1)
    return f"m{n}"

def _find_by_local_id(chat_id: str, local_id: str) -> Optional[Any]:
    if not local_id:
        return None
    for msg in reversed(_state().memory_store.get(chat_id)[-FIND_BY_LOCAL_ID_LIMIT:]):
        if getattr(msg, "local_id", "") == local_id:
            return msg
    return None

def _most_recent_user_local_id(chat_id: str) -> str:
    for msg in reversed(_state().memory_store.get(chat_id)):
        if msg.role == "user":
            return getattr(msg, "local_id", "") or ""
    return ""

def _replace_local_ids_with_text(chat_id: str, text: str) -> str:
    """Replace local message IDs (e.g. m123) with human-readable references.

    Uses re.sub for precise, position-aware replacement instead of str.replace
    which could accidentally replace multiple occurrences.
    """
    if not text:
        return ""

    def _repl(match: re.Match) -> str:
        local_id = match.group(0)
        msg = _find_by_local_id(chat_id, local_id)
        if msg:
            role_text = "我" if msg.role == "user" else "小青"
            return f"{role_text}说过"
        return local_id

    return re.sub(r"(?<![A-Za-z0-9_])m\d{1,6}(?![A-Za-z0-9_])", _repl, text)
