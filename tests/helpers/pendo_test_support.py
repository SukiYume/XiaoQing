"""Pendo 基础测试共享导入和私有 helper。"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from core.interfaces import PluginCapabilities
from plugins.pendo.config import PendoConfig
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


@contextmanager
def managed_pendo_database(tmp_path: Path):
    """创建独立 Pendo 数据库，并确保连接在测试结束时关闭。"""

    from plugins.pendo.services.db import Database

    database = Database(str(tmp_path / "pendo.db"))
    try:
        yield database
    finally:
        database.cleanup()


def reset_pendo_runtime_config() -> None:
    """复位测试修改的进程内配置，不给生产配置类增加测试入口。"""

    with PendoConfig._runtime_lock:
        PendoConfig._runtime_settings = PendoConfig._RUNTIME_DEFAULTS
        PendoConfig._runtime_revision = None


class _ReminderMessageService:
    """返回一条固定提醒，供调度发送与拒绝分支共同使用。"""

    def check_and_send_reminders(self, context=None):
        return {
            "messages": [
                {
                    "user_id": "1001",
                    "message": "提醒消息",
                    "item_id": "evt123",
                    "remind_time": "2030-01-01T09:00:00",
                }
            ]
        }


async def _single_user_shanghai_settings(user_ids, _db):
    """为财务摘要测试返回固定的单用户时区设置。"""
    return {
        user_ids[0]: {
            "settings": {"timezone": "Asia/Shanghai"},
            "custom_settings": {},
        }
    }


def _with_scheduled_delivery_contract(db: Any) -> Any:
    """给轻量数据库替身补齐生产环境使用的周期投递租约接口。"""

    def claim(task_name, owner_id, period_key, *, now, lease_seconds):
        del now, lease_seconds
        return {
            "claim_token": f"lease:{task_name}:{owner_id}:{period_key}",
            "delivery_key": f"delivery:{task_name}:{owner_id}:{period_key}",
        }

    db.claim_scheduled_delivery = claim
    db.complete_scheduled_delivery = lambda *_args: True
    db.release_scheduled_delivery = lambda *_args: True
    return db


def _read_scheduled_delivery(
    db: Any,
    task_name: str,
    owner_id: str,
    period_key: str,
) -> dict[str, Any] | None:
    """测试侧直接读取调度 Outbox，不为只读断言扩张生产仓储接口。"""

    row = (
        db.get_connection()
        .execute(
            """
        SELECT * FROM scheduled_delivery_outbox
        WHERE task_name = ? AND owner_id = ? AND period_key = ?
        """,
            (str(task_name).strip(), str(owner_id).strip(), str(period_key).strip()),
        )
        .fetchone()
    )
    return dict(row) if row is not None else None


class _StubSimpleHandler:
    async def handle(self, user_id, args, context, group_id=None):
        return {"status": "success", "message": "ok"}

    async def search(self, user_id, args, context):
        return {"status": "success", "message": "ok"}


class _StubTaskHandler:
    def __init__(self):
        self.group_ids = []

    async def handle(self, user_id, args, context, group_id=None):
        self.group_ids.append(group_id)
        return {"status": "success", "message": f"group:{group_id}"}


class _StubCaptureHandler:
    def __init__(self):
        self.calls = []

    async def handle(self, user_id, args, context, group_id=None):
        self.calls.append(
            {"user_id": user_id, "args": args, "context": context, "group_id": group_id}
        )
        return {"status": "success", "message": args}


class _StubExporter:
    def export_markdown(self, user_id, args, options):
        return {
            "status": "success",
            "message": "exported",
            "file_path": "C:/tmp/pendo-export.md",
            "file_name": "pendo-export.md",
        }


class _FakeItemsRepo:
    def __init__(self, tasks):
        self._tasks = tasks

    def get_items(self, user_id, filters, limit):
        status = filters.get("status")
        if status is None:
            return list(self._tasks)
        return [task for task in self._tasks if task.status == status]

    def get_all_items(self, user_id, filters):
        return self.get_items(user_id, filters, limit=len(self._tasks))


class _FakeDb:
    def __init__(self, tasks):
        self._repo = _FakeItemsRepo(tasks)

    def get_items(self, user_id, filters, limit):
        return self._repo.get_items(user_id, filters, limit)

    def get_all_items(self, user_id, filters):
        return self._repo.get_all_items(user_id, filters)


def _build_task(task_id: str, category: str, created_at: str):
    return SimpleNamespace(
        id=task_id,
        title=task_id,
        category=category,
        status="done",
        priority=3,
        plan_date=None,
        deadline_at=None,
        created_at=created_at,
    )


__all__ = (
    "Any",
    "ClassVar",
    "Path",
    "PluginCapabilities",
    "ROOT",
    "SimpleNamespace",
    "_FakeDb",
    "_FakeItemsRepo",
    "_ReminderMessageService",
    "_StubCaptureHandler",
    "_StubExporter",
    "_StubSimpleHandler",
    "_StubTaskHandler",
    "_build_task",
    "_single_user_shanghai_settings",
    "_read_scheduled_delivery",
    "_with_scheduled_delivery_contract",
    "asyncio",
    "datetime",
    "json",
    "managed_pendo_database",
    "reset_pendo_runtime_config",
    "timezone",
)
