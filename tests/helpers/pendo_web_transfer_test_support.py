"""Pendo 传输测试共享 fixture、导入和私有 helper。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.models.item import DiaryItem, EventItem, LedgerItem, NoteItem, TaskItem
from plugins.pendo.services.db import Database, DuplicateBundleImportError
from tests.helpers.paths import REPOSITORY_ROOT

try:
    from plugins.pendo.web.auth import issue_login_code
except ModuleNotFoundError:
    pytest.skip("pendo web auth requires PyJWT", allow_module_level=True)


from plugins.pendo.web.api import transfer as transfer_api
from plugins.pendo.web.services import transfer_bundle as transfer_bundle_module
from plugins.pendo.web.services.transfer_bundle import (
    BundleValidationError,
    build_manifest,
    read_bundle,
    serialize_event_collection,
    serialize_item,
    write_bundle,
)

ROOT = REPOSITORY_ROOT


OWNER_ID = "u-transfer"


@pytest.fixture()
def auth_headers(client: Any) -> dict[str, str]:
    """换取测试用户的 CSRF 令牌。"""

    code = issue_login_code(OWNER_ID)
    response = client.post("/api/auth/exchange", json={"code": code})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["data"]["csrf_token"]}


def _seed_items(db: Database) -> None:
    """写入覆盖五类条目和范围内外日期的导出样本。"""

    db.insert_item(
        {
            "id": "event_in",
            "owner_id": OWNER_ID,
            "type": "event",
            "title": "月内会议",
            "category": "工作",
            "start_time": "2026-03-04T10:00:00+08:00",
            "end_time": "2026-03-04T11:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "location": "会议室",
            "participants": ["A", "B"],
            "remind_times": ["2026-03-04T09:30:00+08:00"],
            "notes": "带纪要",
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "event_out",
            "owner_id": OWNER_ID,
            "type": "event",
            "title": "月外会议",
            "category": "工作",
            "start_time": "2026-02-26T10:00:00+08:00",
            "end_time": "2026-02-26T11:00:00+08:00",
            "created_at": "2026-02-25T09:00:00+08:00",
            "updated_at": "2026-02-25T09:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "task_due_in",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "月内待办",
            "content": "有截止时间",
            "category": "工作",
            "plan_date": "2026-03-11",
            "deadline_at": "2026-03-11T18:00:00+08:00",
            "priority": 2,
            "status": "open",
            "remind_times": ["2026-03-11T09:00:00+08:00"],
            "created_at": "2026-03-02T09:00:00+08:00",
            "updated_at": "2026-03-02T09:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "task_fallback_created",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "无截止待办",
            "content": "靠创建时间命中",
            "category": "个人",
            "priority": 3,
            "status": "open",
            "created_at": "2026-03-12T09:00:00+08:00",
            "updated_at": "2026-03-12T09:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "task_out",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "月外待办",
            "content": "不应导出",
            "category": "个人",
            "plan_date": "2026-02-10",
            "deadline_at": "2026-02-10T18:00:00+08:00",
            "priority": 3,
            "status": "open",
            "created_at": "2026-02-02T09:00:00+08:00",
            "updated_at": "2026-02-02T09:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "ledger_in",
            "owner_id": OWNER_ID,
            "type": "ledger",
            "title": "午饭",
            "amount": 23.5,
            "transaction_type": "expense",
            "ledger_category": "餐饮",
            "ledger_date": "2026-03-08",
            "remark": "食堂",
            "created_at": "2026-03-08T12:00:00+08:00",
            "updated_at": "2026-03-08T12:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "ledger_out",
            "owner_id": OWNER_ID,
            "type": "ledger",
            "title": "旧账单",
            "amount": 12,
            "transaction_type": "expense",
            "ledger_category": "交通",
            "ledger_date": "2026-02-08",
            "created_at": "2026-02-08T12:00:00+08:00",
            "updated_at": "2026-02-08T12:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "note_in",
            "owner_id": OWNER_ID,
            "type": "note",
            "title": "月内笔记",
            "content": "笔记正文",
            "category": "知识",
            "tags": ["学习", "导入"],
            "references": [{"kind": "item", "id": "task_due_in"}],
            "related_items": ["event_in"],
            "created_at": "2026-03-09T20:00:00+08:00",
            "updated_at": "2026-03-09T20:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "note_out",
            "owner_id": OWNER_ID,
            "type": "note",
            "title": "旧笔记",
            "content": "过期",
            "category": "知识",
            "created_at": "2026-02-09T20:00:00+08:00",
            "updated_at": "2026-02-09T20:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "diary_in",
            "owner_id": OWNER_ID,
            "type": "diary",
            "title": "月内日记",
            "content": "今天很顺利",
            "diary_date": "2026-03-15",
            "mood": "happy",
            "mood_score": 8,
            "weather": "sunny",
            "location": "上海",
            "template_id": "tpl-1",
            "created_at": "2026-03-15T22:00:00+08:00",
            "updated_at": "2026-03-15T22:00:00+08:00",
        }
    )
    db.insert_item(
        {
            "id": "diary_out",
            "owner_id": OWNER_ID,
            "type": "diary",
            "title": "旧日记",
            "content": "不在范围内",
            "diary_date": "2026-02-15",
            "created_at": "2026-02-15T22:00:00+08:00",
            "updated_at": "2026-02-15T22:00:00+08:00",
        }
    )


def _build_sample_bundle_bytes(
    records_by_type: dict[str, list[dict[str, Any]]],
    selection: dict[str, Any] | None = None,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> bytes:
    files = []
    paths = {
        "event": "data/events.ndjson",
        "task": "data/tasks.ndjson",
        "note": "data/notes.ndjson",
        "ledger": "data/ledger.ndjson",
        "diary": "data/diary.ndjson",
        "event_collection": "data/event_collections.ndjson",
    }
    for item_type, rows in records_by_type.items():
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode(
            "utf-8"
        )
        files.append(
            {
                "path": paths[item_type],
                "type": item_type,
                "count": len(rows),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = build_manifest(
        selection
        or {"types": list(records_by_type.keys()), "preset": "all", "start": None, "end": None},
        files,
        timezone_name,
    )
    buf = io.BytesIO()
    write_bundle(buf, manifest, records_by_type)
    return buf.getvalue()


def _build_raw_bundle(
    manifest: object,
    members: list[tuple[str, str | bytes]],
) -> io.BytesIO:
    """构造可包含故意异常成员的原始 ZIP，供读取边界测试使用。"""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for path, content in members:
            zf.writestr(path, content)
    buf.seek(0)
    return buf


def _build_single_member_manifest(
    item_type: str,
    content: bytes,
    *,
    count: int = 1,
) -> dict[str, Any]:
    """为单个普通数据文件构造完整且自洽的清单。"""

    return build_manifest(
        {"types": [item_type], "preset": "all", "start": None, "end": None},
        [
            {
                "path": transfer_bundle_module.TYPE_FILE_NAMES[item_type],
                "type": item_type,
                "count": count,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )


class _ImportRequest:
    """满足传输 API 直接调用所需最小接口的请求替身。"""

    def __init__(self, body: bytes, headers: dict[str, Any] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    async def body(self) -> bytes:
        return self._body


def _simple_task_bundle(source_id: str) -> bytes:
    """构造只含一条任务的有效传输包。"""

    return _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": source_id,
                    "title": "线程响应性测试",
                    "status": "open",
                }
            ]
        }
    )


__all__ = (
    "Any",
    "BundleValidationError",
    "Database",
    "DiaryItem",
    "DuplicateBundleImportError",
    "EventItem",
    "LedgerItem",
    "NoteItem",
    "OWNER_ID",
    "Path",
    "ROOT",
    "SimpleNamespace",
    "TaskItem",
    "ZoneInfo",
    "_ImportRequest",
    "_build_raw_bundle",
    "_build_sample_bundle_bytes",
    "_build_single_member_manifest",
    "_seed_items",
    "_simple_task_bundle",
    "asyncio",
    "auth_headers",
    "build_manifest",
    "datetime",
    "hashlib",
    "io",
    "json",
    "pytest",
    "re",
    "read_bundle",
    "serialize_event_collection",
    "serialize_item",
    "threading",
    "time",
    "timezone",
    "transfer_api",
    "transfer_bundle_module",
    "write_bundle",
    "zipfile",
)
