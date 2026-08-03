"""Pendo 深层回归测试共享导入和私有 helper。"""

import asyncio
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from core.session import SessionManager
from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.handlers.note import NoteHandler
from plugins.pendo.handlers.task import TaskHandler
from plugins.pendo.models.item import ItemType, TaskStatus
from plugins.pendo.services.ai_parser import AIParser
from plugins.pendo.services.db import Database
from plugins.pendo.services.reminder import ReminderService
from plugins.pendo.utils.db_ops import DbOpsMixin

ROOT = Path(__file__).resolve().parents[2]


class _PendoSessionTestContext:
    plugin_name = "pendo"
    current_user_id = 1001
    current_group_id = None
    logger = logging.getLogger("test.pendo.session")

    def __init__(self, manager: SessionManager, services: dict[str, object]) -> None:
        self.session_manager = manager
        self.state = {"pendo_runtime": {"services": services}}
        self.get_session_calls = 0

    async def get_session(self):
        self.get_session_calls += 1
        raise AssertionError("handle_session must use the transaction Session argument")

    async def create_session(self, initial_data=None, timeout=300.0):
        return await self.session_manager.create(
            self.current_user_id,
            self.current_group_id,
            self.plugin_name,
            initial_data,
            timeout,
        )

    async def end_session(self):
        return await self.session_manager.delete(self.current_user_id, self.current_group_id)


class _SessionEventHandler:
    async def create_event(self, user_id, parsed_data, context, allow_conflict=False):
        _ = (user_id, context, allow_conflict)
        return {
            "status": "need_confirm",
            "message": "需要确认冲突",
            "data": dict(parsed_data),
        }


class _SessionAiParser:
    async def parse_event_with_ai(self, text, user_id, *, partial=False):
        _ = (text, user_id)
        assert partial is True
        return {"start_time": "2030-01-01T10:00:00"}


def _pendo_session_services() -> dict[str, object]:
    return {
        "event_handler": _SessionEventHandler(),
        "ai_parser": _SessionAiParser(),
    }


def _make_temp_db(prefix: str) -> tuple[Path, Database]:
    temp_dir = ROOT / ".pytest_cache" / "tmp" / f"{prefix}_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir, Database(str(temp_dir / "pendo.db"))


def _seed_event_batch_fixture(db: Database, owner_id: str = "u-event-batch") -> list[str]:
    collection_specs = (
        ("aaaabbbb", "项目甲"),
        ("ccccdddd", "项目乙"),
    )
    event_ids: list[str] = []
    for collection_index, (collection_id, title) in enumerate(collection_specs, 1):
        db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": title,
                "category": "工作",
                "start_time": f"2030-01-0{collection_index}T10:00:00",
                "end_time": f"2030-01-0{collection_index}T12:00:00",
            }
        )
        for event_index in range(1, 4):
            event_id = f"{collection_id}_m{event_index:02d}"
            remind_time = f"2030-01-0{collection_index}T0{event_index}:00:00"
            db.insert_item(
                {
                    "id": event_id,
                    "owner_id": owner_id,
                    "type": "event",
                    "title": f"节点 {collection_index}-{event_index}",
                    "category": "工作",
                    "start_time": f"2030-01-0{collection_index}T1{event_index}:00:00",
                    "remind_times": [remind_time],
                    "event_role": "multi_node_child",
                    "event_collection_id": collection_id,
                    "event_collection_kind": "multi_node",
                    "event_index": event_index,
                }
            )
            stored_remind_time = db.get_item(event_id, owner_id).remind_times[0]
            db.confirm_reminder(
                event_id,
                user_action="preconfirmed",
                owner_id=owner_id,
                remind_time=stored_remind_time,
                allow_future=True,
            )
            event_ids.append(event_id)
    return event_ids


__all__ = (
    "AIParser",
    "Any",
    "AsyncMock",
    "Database",
    "DbOpsMixin",
    "EventHandler",
    "ItemType",
    "NoteHandler",
    "Path",
    "ROOT",
    "ReminderService",
    "SessionManager",
    "SimpleNamespace",
    "TaskHandler",
    "TaskStatus",
    "ThreadPoolExecutor",
    "ZoneInfo",
    "_PendoSessionTestContext",
    "_SessionAiParser",
    "_SessionEventHandler",
    "_make_temp_db",
    "_pendo_session_services",
    "_seed_event_batch_fixture",
    "asyncio",
    "datetime",
    "json",
    "logging",
    "pytest",
    "shutil",
    "sqlite3",
    "threading",
    "uuid",
)
