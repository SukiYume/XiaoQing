from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, TypedDict

from core.interfaces import PluginContextProtocol

if TYPE_CHECKING:
    from ..handlers.diary import DiaryHandler
    from ..handlers.event import EventHandler
    from ..handlers.ledger import LedgerHandler
    from ..handlers.note import NoteHandler
    from ..handlers.search import SearchHandler
    from ..handlers.task import TaskHandler
    from ..handlers.web import WebHandler
    from ..services.ai_parser import AIParser
    from ..services.db import Database
    from ..services.exporter import ExporterService
    from ..services.reminder import ReminderService


class PendoContextProtocol(PluginContextProtocol, Protocol):
    async def create_session(
        self, initial_data: dict[str, Any] | None = None, timeout: float | None = 300.0
    ) -> Any: ...

    async def end_session(self) -> bool: ...


PendoContext: TypeAlias = PendoContextProtocol


class SessionData(Protocol):
    """Mutable transaction-local view supplied by ``SessionManager.update``."""

    user_id: int
    group_id: int | None
    plugin_name: str
    session_id: str

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...

    def delete(self, key: str) -> bool: ...

    def clear(self) -> None: ...


CommandMessage: TypeAlias = dict[str, Any]


class PendoServices(TypedDict):
    """同一插件上下文中共享且生命周期一致的服务集合。"""

    db: Database
    ai_parser: AIParser
    reminder_service: ReminderService
    exporter: ExporterService
    event_handler: EventHandler
    task_handler: TaskHandler
    note_handler: NoteHandler
    diary_handler: DiaryHandler
    search_handler: SearchHandler
    ledger_handler: LedgerHandler
    web_handler: WebHandler
