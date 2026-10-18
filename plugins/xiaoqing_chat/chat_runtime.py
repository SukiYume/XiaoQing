"""插件运行时访问：配置热加载、AI 路由上下文、模型调用参数、屏蔽规则与会话锁。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config.config import XiaoQingChatConfig, load_xiaoqing_chat_config
from .llm.llm_config import LLMCallConfig
from .persona import resolve_bot_name
from .runtime_state import ChatRuntime
from .runtime_state import get_state as _state
from .utils.wall_clock import configure_wall_clock


def chat_lock(chat_id: str):
    """取得指定会话的异步锁。"""
    return _state().get_lock(chat_id)


def configured_bot_name(context: Any) -> str:
    """读取并规范化当前机器人名称。"""
    return resolve_bot_name(context.get_settings_snapshot().config.get("bot_name"))


def load_runtime(context: Any) -> ChatRuntime:
    """加载或返回缓存的运行配置，并按实际加载优先级监听配置文件。"""
    settings = context.get_settings_snapshot()
    configure_wall_clock(settings.config.get("timezone"))
    plugin_dir: Path = context.plugin_dir
    config_key       = str(plugin_dir)
    # 监听器必须观察加载器实际读取的路径；config/ 子目录优先于插件根目录。
    config_path_sub  = plugin_dir / "config" / "xiaoqing_config.json"
    config_path_root = plugin_dir / "xiaoqing_config.json"
    if config_path_sub.exists():
        config_path = config_path_sub
    elif config_path_root.exists():
        config_path = config_path_root
    else:
        config_path = config_path_sub  # 文件尚不存在时仍监听约定路径。
    mtime = config_path.stat().st_mtime_ns if config_path.exists() else -1

    state  = _state()
    cached = state.get_runtime(config_key)
    if (
        cached is not None
        and state.get_runtime_mtime(config_key) == mtime
        and state.get_runtime_revision(config_key) == settings.revision
    ):
        return cached

    try:
        cfg = load_xiaoqing_chat_config(context_config=settings.config, plugin_dir=plugin_dir)
        compiled = []
        for pattern in cfg.ban_regex:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                continue

        runtime = ChatRuntime(cfg=cfg, compiled_ban_regex=compiled)
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
    state.set_runtime(config_key, runtime, mtime, settings.revision)
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
            "model": "",
            "_ai": None,
            "_route": "chat",
            "_pinned_model": None,
            "_provider_name": "",
            "_providers": {},
            "_default": "",
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
            "profile": profile,
            "provider": by_profile[profile].provider,
            "model": by_profile[profile].model,
            "modalities": list(by_profile[profile].modalities),
        }
        for alias, profile in aliases.items()
    }
    configured_default = (
        str(ai_config.get("default_model_alias", "") or "").strip()
        if isinstance(ai_config, Mapping)
        else ""
    )
    if configured_default not in providers:
        primary_profile    = model_infos[0].name if model_infos else ""
        configured_default = next(
            (alias for alias, profile in aliases.items() if profile == primary_profile),
            "",
        )

    state                = _state()
    active               = state.resolve_provider_name(chat_id, list(providers), configured_default)
    active_config        = providers.get(active, {})
    local_override       = state.get_chat_provider(chat_id) if chat_id is not None else None
    global_override      = state.global_active_provider
    is_explicit_override = active in {local_override, global_override}

    return {
        "model": active_config.get("model", ""),
        "_ai": ai_service,
        "_route": "chat",
        "_pinned_model": active_config.get("profile") if is_explicit_override else None,
        "_profile": active_config.get("profile", ""),
        "_provider_name": active,
        "_providers": providers,
        "_default": configured_default,
    }


def llm_call_config(
    cfg: XiaoQingChatConfig,
    *,
    foreground: bool = False,
) -> LLMCallConfig:
    """解析插件行为配置中的超时、重试和生成参数。"""
    if foreground:
        timeout        = cfg.foreground_timeout_seconds
        max_retry      = cfg.foreground_max_retry
        retry_interval = cfg.foreground_retry_interval_seconds
    else:
        timeout        = cfg.background_timeout_seconds
        max_retry      = cfg.background_max_retry
        retry_interval = cfg.background_retry_interval_seconds
    return LLMCallConfig(
        timeout_seconds        = timeout,
        max_retry              = max_retry,
        retry_interval_seconds = retry_interval,
        temperature            = float(cfg.temperature),
        top_p                  = float(cfg.top_p),
        max_tokens             = int(cfg.max_tokens),
    )


def is_ignored_text(text: str, runtime: ChatRuntime) -> bool:
    """根据屏蔽词和正则规则判断是否忽略文本。"""
    s = text.strip()
    if not s:
        return True
    for w in runtime.cfg.ban_words:
        if w and w in s:
            return True
    return any(regex.search(s) for regex in runtime.compiled_ban_regex)
