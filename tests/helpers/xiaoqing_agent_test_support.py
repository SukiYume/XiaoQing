"""xiaoqing_agent 测试共享的假模型、假上下文和响应构造。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.ai import AICompletionResult
from core.interfaces import PluginCapabilities, PluginPrincipal, PluginSettingsSnapshot

ADMIN_ID  = 10001
GROUP_ID  = 20002
MEMBER_ID = 30003


def tool_call(name: str, arguments: dict[str, Any] | str, call_id: str = "") -> dict[str, Any]:
    return {
        "id": call_id or f"call-{name}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def ai_response(
    content: str                       = "",
    calls: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content, **extra}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


Scripted = dict[str, Any] | Exception | Callable[[str, list[dict[str, Any]], dict[str, Any]], Any]


class FakeAI:
    """按脚本依次返回响应；脚本项可以是响应、异常或可等待的回调。"""

    def __init__(self, script: list[Scripted] | None = None) -> None:
        self.script                                                        = list(script or [])
        self.calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def complete(
        self,
        route: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AICompletionResult:
        self.calls.append((route, copy.deepcopy(messages), kwargs))
        if not self.script:
            raise AssertionError("FakeAI script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(route, messages, kwargs)
            if asyncio.iscoroutine(item):
                item = await item
        return AICompletionResult(
            response      = item,
            profile       = "fake",
            provider      = "fake",
            model         = "fake-model",
            finish_reason = "stop",
            attempts      = 1,
        )

    def list_models(self, route: str, **_kwargs: Any) -> tuple[Any, ...]:
        return ()


class FakeLocalTools:
    """Core 签发的本机工具授权；``allowed`` 置假模拟撤掉 Bot 管理员。"""

    def __init__(self) -> None:
        self.allowed = True

    def ensure_authorized(self) -> None:
        if not self.allowed:
            raise PermissionError("agent capability is no longer authorized for this user")


class FakeAgentContext:
    def __init__(
        self,
        data_dir: Path,
        *,
        ai: FakeAI | None         = None,
        agent_config: dict | None = None,
        user_id: int              = ADMIN_ID,
        group_id: int | None      = None,
        is_admin: bool            = True,
        group_role: str           = "member",
        plugins: tuple[str, ...]  = ("xiaoqing_agent", "xiaoqing_chat"),
        catalog: tuple[Any, ...]  = (),
        **capabilities: Any,
    ) -> None:
        self.plugin_name                   = "xiaoqing_agent"
        self.data_dir                      = data_dir
        self.state: dict                   = {}
        self.current_user_id               = user_id
        self.current_group_id              = group_id
        self.request_id                    = "test-request"
        self.command_invocation            = None
        self.actions: list[dict[str, Any]] = []
        self.ai                            = ai or FakeAI()
        self.agent_config                  = {"enabled": True, **(agent_config or {})}
        self.extra_config: dict[str, Any]  = {}
        self._plugins                      = plugins
        self._catalog                      = catalog
        if is_admin:
            capabilities.setdefault("local_tools", FakeLocalTools())
        self.capabilities = PluginCapabilities(is_bot_admin=is_admin, ai=self.ai, **capabilities)
        self.principal = PluginPrincipal(
            kind="user",
            user_id=user_id,
            group_id=group_id,
            is_bot_admin=is_admin,
            is_private=group_id is None,
            group_role=group_role if group_id is not None else "unknown",  # type: ignore[arg-type]
        )
        self.send_results: list[bool | None] = []

    def with_capabilities(self, **updates: Any) -> FakeAgentContext:
        self.capabilities = replace(self.capabilities, **updates)
        return self

    def get_settings_snapshot(self) -> PluginSettingsSnapshot:
        plugin = {**self.agent_config, **self.extra_config}
        return PluginSettingsSnapshot(
            config={
                "bot_name": "小青",
                "timezone": "Asia/Shanghai",
                "command_prefixes": ["/"],
                "plugins": {"xiaoqing_agent": plugin},
            },
            secrets  = {},
            revision = 0,
        )

    def get_config(self, path: str) -> Any:
        current: Any = self.get_settings_snapshot().config["plugins"]["xiaoqing_agent"]
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    async def send_action(self, action: dict[str, Any]) -> bool | None:
        self.actions.append(action)
        return self.send_results.pop(0) if self.send_results else True

    def list_plugins(self) -> list[str]:
        return list(self._plugins)

    def get_command_catalog(self) -> tuple[Any, ...]:
        return self._catalog


def sent_texts(context: FakeAgentContext) -> list[str]:
    texts: list[str] = []
    for action in context.actions:
        message = action.get("params", {}).get("message") or []
        texts.append(
            "".join(
                str(segment.get("data", {}).get("text", ""))
                for segment in message
                if segment.get("type") == "text"
            )
        )
    return texts


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)
