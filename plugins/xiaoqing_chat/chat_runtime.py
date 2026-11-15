"""插件运行时访问：配置热加载、AI 路由上下文、模型调用参数、屏蔽规则与会话锁。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.plugin_base import resolve_bot_name

from .config.config import XiaoQingChatConfig, behavior_config_path, load_xiaoqing_chat_config
from .llm.llm_config import LLMCallConfig
from .persona import persona_card_path
from .runtime_state import ChatRuntime
from .runtime_state import get_state as _state
from .utils.wall_clock import configure_wall_clock


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.exists() else -1


def _persona_mtime(plugin_dir: Path, cfg: XiaoQingChatConfig) -> int:
    return _mtime_ns(persona_card_path(plugin_dir, cfg.personality.persona_file))


def chat_lock(chat_id: str):
    """取得指定会话的异步锁。"""
    return _state().get_lock(chat_id)


def optional_capability(context: Any, name: str) -> Any:
    """框架提供给插件的某项能力（如 ``agent_turn``、``life_context``）；没有这项能力时是 ``None``。"""

    return getattr(getattr(context, "capabilities", None), name, None)


def configured_bot_name(context: Any) -> str:
    """读取并规范化当前机器人名称。"""
    return resolve_bot_name(context.get_settings_snapshot().config.get("bot_name"))


def load_runtime(context: Any) -> ChatRuntime:
    """加载或返回缓存的运行配置；行为配置文件或人设卡改动、部署配置变化时重新加载。"""
    settings = context.get_settings_snapshot()
    configure_wall_clock(settings.config.get("timezone"))
    plugin_dir: Path = context.plugin_dir
    config_key       = str(plugin_dir)
    config_mtime     = _mtime_ns(behavior_config_path(plugin_dir))

    state  = _state()
    cached = state.get_runtime(config_key)
    # 改配置文件或人设卡都在下一条消息生效。
    if (
        cached is not None
        and state.get_runtime_revision(config_key) == settings.revision
        and state.get_runtime_mtime(config_key)
        == (config_mtime, _persona_mtime(plugin_dir, cached.cfg))
    ):
        return cached

    try:
        cfg = load_xiaoqing_chat_config(
            context_config=settings.config, plugin_dir=plugin_dir, logger=context.logger
        )
        # ban_regex 在配置校验时已经确认都能编译。
        runtime = ChatRuntime(cfg=cfg, compiled_ban_regex=[re.compile(p) for p in cfg.ban_regex])
        from .memory.knowledge_base import ensure_knowledge_index

        ensure_knowledge_index(
            memory_db  = state.memory_db,
            data_dir   = context.data_dir,
            plugin_dir = plugin_dir,
            files      = cfg.knowledge.files if cfg.knowledge.enable_knowledge else (),
        )
    except Exception as exc:
        if cached is None:
            raise
        context.logger.warning(
            "xiaoqing_chat config reload retained previous version: %s", type(exc).__name__
        )
        return cached
    state.set_runtime(
        config_key, runtime, (config_mtime, _persona_mtime(plugin_dir, cfg)), settings.revision
    )
    return runtime


def ai_route_context(context: Any, *, chat_id: str | None = None) -> dict[str, Any]:
    """返回不含凭据的 AI route 句柄和当前模型选择。

    provider 地址、密钥、模型参数和默认 fallback 链都由 core 管理。这里仅把公开模型
    profile 映射成 ``/xc 模型`` 沿用的短名称。没有运行时覆盖时不固定模型，因此 core
    会按 route 列表自动降级；管理员显式切换后才通过 ``_pinned_model`` 严格固定。
    """

    ai_service = context.capabilities.ai
    if ai_service is None:
        return {
            "model"         : "",
            "_ai"           : None,
            "_route"        : "chat",
            "_pinned_model" : None,
            "_provider_name": "",
            "_providers"    : {},
            "_default"      : "",
        }

    model_infos = ai_service.list_models("chat")
    by_profile  = {item.name: item for item in model_infos}

    plugin_config  = context.get_settings_snapshot().plugin_config("xiaoqing_chat")
    ai_config      = plugin_config.get("ai", {}) if isinstance(plugin_config, Mapping) else {}
    aliases_config = ai_config.get("model_aliases", {}) if isinstance(ai_config, Mapping) else {}

    aliases: dict[str, str] = {}
    if isinstance(aliases_config, Mapping):
        for raw_alias, raw_profile in aliases_config.items():
            alias   = str(raw_alias or "").strip()
            profile = str(raw_profile or "").strip()
            if alias and profile in by_profile and alias not in aliases:
                aliases[alias] = profile

    # 没有显式短名称的 profile 仍可直接选择，避免配置新增模型后管理命令看不到。
    for info in model_infos:
        if info.name not in aliases.values():
            aliases[info.name] = info.name

    providers = {
        alias: {
            "profile"   : profile,
            "provider"  : by_profile[profile].provider,
            "model"     : by_profile[profile].model,
            "modalities": list(by_profile[profile].modalities),
        }
        for alias, profile in aliases.items()
    }
    primary_profile    = model_infos[0].name if model_infos else ""
    configured_default = next(
        (alias for alias, profile in aliases.items() if profile == primary_profile), ""
    )

    state                = _state()
    active               = state.resolve_provider_name(chat_id, list(providers), configured_default)
    active_config        = providers.get(active, {})
    local_override       = state.get_chat_provider(chat_id) if chat_id is not None else None
    global_override      = state.global_active_provider
    is_explicit_override = active in {local_override, global_override}

    return {
        "model"         : active_config.get("model", ""),
        "_ai"           : ai_service,
        "_route"        : "chat",
        "_pinned_model" : active_config.get("profile") if is_explicit_override else None,
        "_profile"      : active_config.get("profile", ""),
        "_provider_name": active,
        "_providers"    : providers,
        "_default"      : configured_default,
    }


def llm_call_config(
    cfg: XiaoQingChatConfig,
    *,
    foreground: bool = False,
) -> LLMCallConfig:
    """从主配置选取前台聊天或后台推理路由的请求参数。"""
    return cfg.llm_config("chat" if foreground else "reasoning")


def is_ignored_text(text: str, runtime: ChatRuntime) -> bool:
    """根据屏蔽词和正则规则判断是否忽略文本。"""
    s = text.strip()
    if not s:
        return True
    for w in runtime.cfg.ban_words:
        if w and w in s:
            return True
    return any(regex.search(s) for regex in runtime.compiled_ban_regex)
