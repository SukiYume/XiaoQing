# Pendo 运行期服务：数据库、Web 和重载钩子由同一生命周期持有。
"""State-owning lifecycle boundary for Pendo's database, Web UI and reload hook."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from core.public_errors import public_error_message

from ..config import PendoRuntimeSettings
from ..utils.db_ops import (
    claim_database_singleton,
    detach_database_singleton,
    resolve_database_path,
)

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


class _WebBackend(Protocol):
    def is_running(self) -> bool: ...

    def start(self, db: Database) -> bool: ...

    def stop(self) -> bool: ...


def _load_web_backend() -> _WebBackend:
    from ..web import server as web_server

    return cast(_WebBackend, web_server)


@dataclass(frozen=True, slots=True)
class RuntimeCleanupFailure:
    component: str
    error: Exception


class PendoRuntimeService:
    """Own resources that must be acquired and released as one plugin generation."""

    def __init__(
        self,
        *,
        web_backend_factory: Callable[[], _WebBackend] = _load_web_backend,
    ) -> None:
        self._database: Database | None                     = None
        self._database_path: Path | None                    = None
        self._config_unsubscribe: Callable[[], None] | None = None
        self._web_backend_factory                           = web_backend_factory

    @property
    def database(self) -> Database | None:
        return self._database

    @property
    def database_path(self) -> Path | None:
        return self._database_path

    @property
    def has_config_subscription(self) -> bool:
        return self._config_unsubscribe is not None

    def open_database(
        self,
        context: Any,
        database_factory: Callable[[str], Database],
    ) -> Database:
        """Create and publish exactly one database for this plugin generation."""

        if self._database is not None:
            raise RuntimeError("Pendo runtime database is already initialized")
        db_path  = resolve_database_path(context)
        database = database_factory(str(db_path))
        try:
            self.adopt_database(database, database_path=db_path)
        except BaseException:
            try:
                database.cleanup()
            except Exception as cleanup_exc:
                public_error_message(
                    None,
                    cleanup_exc,
                    logger    = logger,
                    component = "pendo.runtime.database_candidate_cleanup",
                )
            raise
        return database

    def adopt_database(
        self,
        database: Database,
        *,
        database_path: Path | None = None,
    ) -> None:
        """Claim an already-created database for one lifecycle generation."""

        if self._database is not None and self._database is not database:
            raise RuntimeError("Pendo runtime database is already initialized")
        claim_database_singleton(database)
        self._database      = database
        self._database_path = database_path

    def bind_config_subscription(self, unsubscribe: Callable[[], None]) -> bool:
        """Publish one unsubscribe callback without replacing an active owner."""

        if not callable(unsubscribe):
            raise TypeError("config subscription must return an unsubscribe callback")
        if self._config_unsubscribe is not None:
            return False
        self._config_unsubscribe = unsubscribe
        return True

    def unsubscribe_config(self) -> None:
        unsubscribe, self._config_unsubscribe = self._config_unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def close_databases(self) -> tuple[RuntimeCleanupFailure, ...]:
        """Detach every owned database first, then close each object exactly once."""

        owned, self._database = self._database, None
        self._database_path                   = None
        singleton                             = detach_database_singleton()
        failures: list[RuntimeCleanupFailure] = []
        seen: set[int]                        = set()
        for component, database in (
            ("runtime_db", owned),
            ("singleton_db", singleton),
        ):
            if database is None or id(database) in seen:
                continue
            seen.add(id(database))
            try:
                database.cleanup()
            except Exception as exc:
                failures.append(RuntimeCleanupFailure(component, exc))
        return tuple(failures)

    def start_web(self, db: Database) -> bool:
        """Start the Web UI after replacing a server owned by the same process."""

        try:
            web_server = self._web_backend_factory()
            if web_server.is_running():
                stopped = web_server.stop()
                if not stopped and web_server.is_running():
                    logger.warning("Pendo Web UI is still running; skip restart")
                    return False

            started = web_server.start(db)
            if not started:
                logger.warning("Failed to auto-start web UI")
            return bool(started)
        except Exception as exc:
            public_error_message(None, exc, logger=logger, component="pendo.web.start")
            return False

    def stop_web(self) -> bool:
        try:
            return bool(self._web_backend_factory().stop())
        except Exception as exc:
            public_error_message(None, exc, logger=logger, component="pendo.web.stop")
            return False

    def reconfigure_web(
        self,
        db: Database,
        before: PendoRuntimeSettings,
        after: PendoRuntimeSettings,
    ) -> None:
        """Apply one published endpoint generation without running two endpoints."""

        endpoint_changed = (before.web_host, before.web_port) != (
            after.web_host,
            after.web_port,
        )
        if before.web_enabled and (not after.web_enabled or endpoint_changed):
            self.stop_web()
        if after.web_enabled and (not before.web_enabled or endpoint_changed):
            self.start_web(db)

    async def stop_web_async(self) -> None:
        await asyncio.to_thread(self.stop_web)
