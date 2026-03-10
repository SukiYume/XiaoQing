from __future__ import annotations

from typing import Any, cast

from .types import PendoContext, PendoServices


def get_plugin_runtime_state(
    context: PendoContext | None, *, create: bool = True
) -> dict[str, Any]:
    if context is not None and isinstance(context.state, dict):
        runtime_state = context.state.get("pendo_runtime")
        if isinstance(runtime_state, dict):
            return runtime_state
        if create:
            context.state["pendo_runtime"] = {}
            return context.state["pendo_runtime"]
    return {}


def get_cached_services(context: PendoContext | None) -> PendoServices | None:
    runtime_state = get_plugin_runtime_state(context, create=False)
    services = runtime_state.get("services")
    if isinstance(services, dict):
        return cast(PendoServices, cast(object, services))
    return None


def set_cached_services(context: PendoContext | None, services: PendoServices) -> None:
    runtime_state = get_plugin_runtime_state(context)
    runtime_state["services"] = services
