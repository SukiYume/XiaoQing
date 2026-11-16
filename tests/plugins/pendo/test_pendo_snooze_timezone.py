# 验证稍后提醒按用户时区计算并保留真实时刻。
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.commands.operations import _parse_snooze_time
from plugins.pendo.handlers.task import TaskHandler
from plugins.pendo.utils import time_utils
from tests.helpers.pendo_test_support import freeze_pendo_clock


def test_relative_snooze_uses_user_aware_instant_not_server_timezone() -> None:
    user_now = datetime(2030, 7, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = datetime.fromisoformat(_parse_snooze_time("10m", now=user_now))
    assert result.tzinfo is not None
    assert result.astimezone(UTC) - user_now.astimezone(UTC) == (result - user_now)
    assert (result - user_now).total_seconds() == 600


def test_absolute_snooze_uses_user_wall_clock_and_offset() -> None:
    # 与服务器所在时区无关：注入的用户当前时刻自带用户时区。
    user_now = datetime(2030, 3, 10, 18, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = datetime.fromisoformat(_parse_snooze_time("19:00", now=user_now))
    assert result.hour == 19 and result.minute == 0
    assert result.utcoffset() == user_now.utcoffset()
    assert (result - user_now).total_seconds() == 30 * 60


@pytest.mark.asyncio
async def test_task_timezone_and_deadline_reschedule(db, monkeypatch):
    db.update_user_settings("u", {"timezone": "Asia/Shanghai"})
    iid = db.insert_item(
        {
            "owner_id"      : "u",
            "type"          : "task",
            "title"         : "future",
            "status"        : "open",
            "deadline_at"   : "2030-01-01T12:00:00+08:00",
            "reminder_rules": [{"offset_seconds": 3600}],
        }
    )
    handler = TaskHandler(db)
    # 用户时区（上海）的 10:00，早于截止时间 12:00。
    freeze_pendo_clock(monkeypatch, datetime(2030, 1, 1, 2, tzinfo=UTC), time_utils)
    overdue = await handler.list_tasks("u", "overdue", None)
    assert overdue["status"] == "success"
    assert "future" not in overdue["message"]
    result = await handler.edit_task("u", iid + " deadline:2030-01-02T12:00", None)
    assert result["status"] == "success"
    assert db.get_item(iid, "u").remind_times == ["2030-01-02T03:00:00+00:00"]
