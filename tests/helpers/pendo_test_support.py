"""Pendo 基础测试共享导入和私有 helper。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.interfaces import PluginCapabilities
from plugins.pendo.config import PendoConfig
from plugins.pendo.utils import db_ops
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


class PendoClock:
    """可手动拨动的固定时钟：``instant`` 就是被冻结模块里 ``datetime.now`` 读到的时刻。"""

    def __init__(self, instant: datetime) -> None:
        self.instant = instant


class _ClockType(type):
    """让真实的 ``datetime`` 实例通过对冻结类的 ``isinstance`` 检查。"""

    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, datetime)


def freeze_pendo_clock(
    monkeypatch: pytest.MonkeyPatch,
    instant: datetime,
    *modules: Any,
) -> PendoClock:
    """时钟是产品之外的边界：把 ``modules`` 里的 ``datetime.now`` 固定在 ``instant``。

    返回的时钟可以改写 ``instant`` 来推进时间；带时区的 ``now(tz)`` 按 ``tz`` 换算，
    不带时区的 ``now()`` 取 ``instant`` 的本地墙钟。
    """

    clock = PendoClock(instant)

    class _FrozenDateTime(datetime, metaclass=_ClockType):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return clock.instant.astimezone(tz)
            return clock.instant.replace(tzinfo=None)

    for module in modules:
        monkeypatch.setattr(module, "datetime", _FrozenDateTime)
    return clock


def freeze_pendo_reminder_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> PendoClock:
    """冻结提醒与定时任务链路读取当前时刻的全部模块（时间工具、提醒服务、定时命令、提醒存储）。"""

    from plugins.pendo.commands import scheduled as scheduled_module
    from plugins.pendo.services import db_reminders
    from plugins.pendo.services import reminder as reminder_module
    from plugins.pendo.utils import time_utils

    return freeze_pendo_clock(
        monkeypatch, instant, time_utils, reminder_module, scheduled_module, db_reminders
    )


@contextmanager
def pendo_service_context(
    db: Any,
    data_dir: Path,
    **attributes: Any,
) -> Iterator[SimpleNamespace]:
    """真实服务集合的运行上下文：把 ``db`` 装成 Pendo 数据库单例，退出时清掉。

    ``attributes`` 是测试要观察的额外上下文属性，例如 ``send_action`` 或 ``end_session``。
    """

    db_ops.set_database_singleton(db)
    context = SimpleNamespace(
        state    = {},
        data_dir = data_dir,
        **attributes,
    )
    try:
        yield context
    finally:
        db_ops.set_database_singleton(None)


def attach_action_recorder(context: Any) -> list[dict]:
    """给上下文装上记录动作、一律回执成功的 ``send_action``，返回动作记录。"""

    sent: list[dict] = []

    async def send_action(action):
        sent.append(action)
        return True

    context.send_action = send_action
    return sent


def reject_inserts(
    db: Any,
    table: str,
    message: str,
    *,
    action: str | None = None,
) -> str:
    """让存储层拒绝向 ``table`` 插入行并以 ``message`` 中止，模拟写入在前序改动之后失败。

    给出 ``action`` 时只拒绝 ``action`` 列等于它的行（用于操作日志）。返回触发器名，
    测试要“恢复存储”时用 ``DROP TRIGGER`` 删掉它。
    """

    name       = f"reject_{table}_{action or 'insert'}"
    guard      = f"WHEN NEW.action = '{action}' " if action is not None else ""
    connection = db.get_connection()
    with connection:
        connection.execute(
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            f"{guard}BEGIN SELECT RAISE(ABORT, '{message}'); END"
        )
    return name


class ReminderMessageService:
    """返回一条固定提醒，供调度发送与拒绝分支共同使用。"""

    def check_and_send_reminders(self):
        return {
            "messages": [
                {
                    "user_id"    : "1001",
                    "message"    : "提醒消息",
                    "item_id"    : "evt123",
                    "remind_time": "2030-01-01T09:00:00",
                }
            ]
        }


def read_scheduled_delivery(
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


__all__ = (
    "Any",
    "Path",
    "PendoClock",
    "PluginCapabilities",
    "ROOT",
    "ReminderMessageService",
    "read_scheduled_delivery",
    "attach_action_recorder",
    "datetime",
    "freeze_pendo_clock",
    "freeze_pendo_reminder_clock",
    "managed_pendo_database",
    "pendo_service_context",
    "reject_inserts",
    "reset_pendo_runtime_config",
)
