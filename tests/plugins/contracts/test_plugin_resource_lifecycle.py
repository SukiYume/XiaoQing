"""插件自有资源的额度与上限：每日额度、有界文件缓存、客户端注册表、论坛缓存。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.bounded_http import BoundedHttpResponse
from plugins.adnmb import adapi
from plugins.adnmb import main as adnmb_main
from plugins.adnmb.adapi import MAX_FORUM_CACHE_ENTRIES, AdnmbClient
from plugins.chat import main as chat_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.settings_snapshot import with_settings_reader


def _business_day(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _chat_context(
    *,
    data_dir: Path,
    user_limit: int             = 1,
    global_limit: int           = 10,
    now: Callable[[], datetime] = lambda: _business_day(14),
) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            now    = now,
            config = {
                "timezone": "Asia/Shanghai",
                "plugins" : {
                    "chat": {
                        "daily_user_limit"  : user_limit,
                        "daily_global_limit": global_limit,
                    }
                },
            },
            http_session = object(),
            data_dir     = data_dir,
            secrets      = {
                "plugins": {
                    "chat": {
                        "token" : "token",
                        "bot_id": "bot",
                    }
                }
            },
            state={},
        )
    )


@pytest.mark.asyncio
async def test_chat_quota_resets_at_business_date_boundary(monkeypatch, tmp_path):
    current_day = [14]
    context = _chat_context(data_dir=tmp_path, now=lambda: _business_day(current_day[0]))
    calls = 0

    async def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"messages": [{"type": "answer", "content": "ok"}]}

    monkeypatch.setattr(chat_main, "call_coze_api", fake_call)

    first          = await chat_main.handle("chat", "first", {"user_id": 7}, context)
    denied         = await chat_main.handle("chat", "second", {"user_id": 7}, context)
    current_day[0] = 15
    next_day       = await chat_main.handle("chat", "third", {"user_id": 7}, context)

    assert text_segments_text(first) == "ok"
    assert "额度已用完" in text_segments_text(denied)
    assert text_segments_text(next_day) == "ok"
    assert calls == 2
    assert chat_main.AtomicJsonStore(tmp_path / "chat_quota.json").read(None) == {
        "window": "2026-07-15",
        "users" : {"7": 1},
        "total" : 1,
    }


@pytest.mark.asyncio
async def test_chat_failed_remote_call_rolls_back_reserved_quota(monkeypatch, tmp_path):
    context = _chat_context(data_dir=tmp_path)
    responses = [None, {"messages": [{"type": "answer", "content": "retry-ok"}]}]

    async def fake_call(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(chat_main, "call_coze_api", fake_call)

    failed = await chat_main.handle("chat", "first", {"user_id": 8}, context)
    assert "对话失败" in text_segments_text(failed)
    assert chat_main.AtomicJsonStore(tmp_path / "chat_quota.json").read(None) == {
        "window": "2026-07-14",
        "users" : {},
        "total" : 0,
    }

    retried = await chat_main.handle("chat", "retry", {"user_id": 8}, context)
    assert text_segments_text(retried) == "retry-ok"
    assert chat_main.AtomicJsonStore(tmp_path / "chat_quota.json").read(None)["users"] == {"8": 1}


@pytest.mark.asyncio
async def test_chat_concurrent_reservations_cannot_oversubscribe(monkeypatch, tmp_path):
    context = _chat_context(data_dir=tmp_path, user_limit=1, global_limit=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls   = 0

    async def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"messages": [{"type": "answer", "content": "ok"}]}

    monkeypatch.setattr(chat_main, "call_coze_api", fake_call)

    first_task = asyncio.create_task(chat_main.handle("chat", "first", {"user_id": 9}, context))
    await entered.wait()
    second = await chat_main.handle("chat", "second", {"user_id": 9}, context)
    release.set()
    first = await first_task

    assert text_segments_text(first) == "ok"
    assert "额度已用完" in text_segments_text(second)
    assert calls == 1


def test_bounded_file_cache_enforces_lru_ttl_entry_and_byte_limits(tmp_path):
    entry_cache = BoundedFileCache(
        tmp_path / "entries",
        FileCacheLimits(max_entries=2, max_bytes=100, ttl_seconds=60),
    )
    first  = entry_cache.put("first.jpg", b"1")
    second = entry_cache.put("second.jpg", b"2")
    assert first is not None and second is not None
    old = time.time() - 10
    os.utime(first, (old, old))
    os.utime(second, (old + 1, old + 1))
    assert entry_cache.get_any(("first.jpg",)) == first
    assert entry_cache.put("third.jpg", b"3") is not None
    assert first.exists()
    assert not second.exists()

    ttl_cache = BoundedFileCache(
        tmp_path / "ttl",
        FileCacheLimits(max_entries=5, max_bytes=100, ttl_seconds=1),
    )
    stale = ttl_cache.put("stale.png", b"old")
    assert stale is not None
    expired = time.time() - 5
    os.utime(stale, (expired, expired))
    ttl_cache.prune()
    assert not stale.exists()

    byte_cache = BoundedFileCache(
        tmp_path / "bytes",
        FileCacheLimits(max_entries=5, max_bytes=5, ttl_seconds=60),
    )
    assert byte_cache.put("a.webp", b"aaa") is not None
    assert byte_cache.put("b.webp", b"bbb") is not None
    assert sum(path.stat().st_size for path in (tmp_path / "bytes").glob("*.*")) <= 5
    assert byte_cache.put("oversized.webp", b"123456") is None


class _SharedSession:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _adnmb_context(tmp_path: Path) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            current_user_id = None,
            http_session    = _SharedSession(),
            secrets         = {"plugins": {"adnmb": {"uuid": "shared"}}},
            state           = {},
            data_dir        = tmp_path / "data",
        )
    )


@pytest.mark.asyncio
async def test_adnmb_client_registry_evicts_lru_and_shutdown_closes_wrappers_only(tmp_path):
    context   = _adnmb_context(tmp_path)
    cache_dir = context.data_dir / "images"
    limit     = adnmb_main.MAX_CACHED_CLIENTS

    clients = [
        adnmb_main._get_client(context, cache_dir, user_id=str(index)) for index in range(limit + 1)
    ]

    assert clients[0].closed is True
    assert all(not client.closed for client in clients[1:])
    assert len(context.state["adnmb_runtime"]["clients"]) == limit

    await adnmb_main.shutdown(context)

    assert all(client.closed for client in clients)
    assert context.http_session.close_calls == 0
    assert "adnmb_runtime" not in context.state


def test_adnmb_client_registry_expires_idle_entries(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(adnmb_main, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    context   = _adnmb_context(tmp_path)
    cache_dir = context.data_dir / "images"

    first = adnmb_main._get_client(context, cache_dir, user_id="1")
    registry = context.state["adnmb_runtime"]["clients"]
    clock[0] += adnmb_main.CLIENT_IDLE_TTL_SECONDS
    second = adnmb_main._get_client(context, cache_dir, user_id="2")

    assert first.closed is True
    assert second.closed is False
    assert tuple(registry) == ("client:2",)


@pytest.mark.asyncio
async def test_adnmb_forum_cache_has_ttl_capacity_and_copy_boundary(monkeypatch, tmp_path):
    client = AdnmbClient(session=object(), cache_dir=tmp_path, uuid="uuid")
    calls  = 0
    clock  = [5000.0]
    forums = [
        {"name": f"forum-{index}", "id": index} for index in range(MAX_FORUM_CACHE_ENTRIES + 5)
    ]
    body = json.dumps([{"forums": forums}]).encode()

    async def fake_request(_session, _method, url, **_kwargs):
        nonlocal calls
        calls += 1
        return BoundedHttpResponse(
            url           = url,
            status        = 200,
            body          = body,
            media_type    = "application/json",
            charset       = "utf-8",
            headers       = {"Content-Type": "application/json"},
            wire_bytes    = len(body),
            decoded_bytes = len(body),
        )

    monkeypatch.setattr(adapi, "aiohttp_request_bounded", fake_request)
    monkeypatch.setattr(
        adapi,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )

    first            = await client.get_forum_list()
    first["mutated"] = "outside"
    cached           = await client.get_forum_list()
    assert calls == 1
    assert len(cached) == MAX_FORUM_CACHE_ENTRIES
    assert "mutated" not in cached

    clock[0] += adapi.FORUM_CACHE_TTL_SECONDS
    await client.get_forum_list()
    assert calls == 2
