from __future__ import annotations

from typing import Any, cast

from .types import PendoContext, PendoServices


def get_plugin_runtime_state(
    context: PendoContext | None, *, create: bool = True
) -> dict[str, Any]:
    """取得绑定到插件上下文的 Pendo 私有状态，不创建全局后备状态。"""
    if context is not None and isinstance(context.state, dict):
        runtime_state = context.state.get("pendo_runtime")
        if isinstance(runtime_state, dict):
            return runtime_state
        if create:
            context.state["pendo_runtime"] = {}
            return context.state["pendo_runtime"]
    return {}


def get_cached_services(context: PendoContext | None) -> PendoServices | None:
    """读取已经发布的服务集合；不完整或错误类型一律视为未缓存。"""
    runtime_state = get_plugin_runtime_state(context, create=False)
    services = runtime_state.get("services")
    if isinstance(services, dict):
        return cast(PendoServices, cast(object, services))
    return None
