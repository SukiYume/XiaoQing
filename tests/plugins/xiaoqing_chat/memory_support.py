"""给聊天测试接上一个真的统一记忆：临时目录里的 xiaoqing_memory，和 Core 一样写入调用方与范围。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.capabilities import MEMORY_METHODS, MemoryService
from plugins.xiaoqing_memory import main as memory_main
from plugins.xiaoqing_memory.model import GLOBAL, Note
from plugins.xiaoqing_memory.store import MemoryStore


class MemoryHarness:
    def __init__(self, root: Path) -> None:
        self.context = SimpleNamespace(
            data_dir              = Path(root) / "xiaoqing_memory",
            state                 = {},
            get_settings_snapshot = lambda: SimpleNamespace(
                config={"bot_name": "小青", "plugins": {"xiaoqing_memory": {"enabled": True}}}
            ),
            capabilities=SimpleNamespace(ai=None),
        )

    @property
    def store(self) -> MemoryStore:
        return memory_main.get_runtime(self.context).store

    def service(self, scope: str, caller: str = "xiaoqing_chat") -> MemoryService:
        def method(name: str) -> Any:
            async def invoke(payload: dict[str, Any]) -> Any:
                return await getattr(memory_main, name)(dict(payload), self.context)

            return invoke

        return MemoryService(
            caller,
            "user" if scope else "system",
            scope,
            {name: method(name) for name in MEMORY_METHODS},
        )

    def notes(self, *scopes: str, kinds: tuple[str, ...] = ()) -> list[Note]:
        found = self.store.notes_for(scopes or (GLOBAL,)).values()
        return sorted(
            (note for note in found if not kinds or note.kind in kinds),
            key=lambda note: note.created_at,
        )


def chat_context(harness: MemoryHarness, data_dir: Path, scope: str, **extra: Any) -> Any:
    """只带统一记忆能力的聊天上下文（投递后抽取、主动开口等后台路径用）。"""

    return SimpleNamespace(
        data_dir=data_dir, capabilities=SimpleNamespace(memory=harness.service(scope)), **extra
    )


def put_history(state: Any, chat_id: str, messages: list[Any]) -> list[Any]:
    """把消息放进聊天的近期历史（推送给统一记忆的就是这份），返回刚放进去的那几条。"""

    for message in messages:
        state.memory_store.append(
            chat_id,
            role          = message.role,
            name          = message.name,
            user_id       = message.user_id,
            content       = message.content,
            ts            = message.ts,
            source_plugin = message.source_plugin,
        )
    return state.memory_store.get(chat_id)[-len(messages) :] if messages else []


def self_texts(harness: MemoryHarness, scope: str, *, kinds: tuple[str, ...] = ()) -> list[str]:
    """在 ``scope`` 里取用到的、她自己仍然有效的记录。"""

    from plugins.xiaoqing_memory.config import MemoryConfig
    from plugins.xiaoqing_memory.recall import recall

    result = recall(
        harness.store,
        MemoryConfig(),
        {"purpose": "self", "self_limit": 12},
        caller = "xiaoqing_chat",
        scope  = scope,
    )
    entries = [*result["self_entries"], *result["stories"]]
    return [entry["text"] for entry in entries if not kinds or entry["kind"] in kinds]


def attach(context: Any, harness: MemoryHarness, scope: str) -> Any:
    """把记忆能力挂到测试上下文上（``capabilities`` 是 SimpleNamespace 或 PluginCapabilities）。"""

    capabilities = getattr(context, "capabilities", None)
    service      = harness.service(scope)
    if capabilities is None:
        context.capabilities = SimpleNamespace(memory=service)
    elif isinstance(capabilities, SimpleNamespace):
        capabilities.memory = service
    else:
        from dataclasses import replace

        context.capabilities = replace(capabilities, memory=service)
    return context
