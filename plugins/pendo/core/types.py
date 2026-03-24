from __future__ import annotations

from typing import Any, Protocol, TypedDict, TypeAlias

from core.interfaces import PluginContextProtocol


class PendoContextProtocol(PluginContextProtocol, Protocol):
    async def create_session(
        self, initial_data: dict[str, Any] | None = None, timeout: float = 300.0
    ) -> Any: ...

    async def end_session(self) -> bool: ...


PendoContext: TypeAlias = PendoContextProtocol
SessionData: TypeAlias = dict[str, Any]
CommandMessage: TypeAlias = dict[str, Any]


class PendoServices(TypedDict):
    db: Any
    ai_parser: Any
    reminder_service: Any
    exporter: Any
    event_handler: Any
    task_handler: Any
    note_handler: Any
    diary_handler: Any
    search_handler: Any
    ledger_handler: Any
