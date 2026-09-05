"""CR-276 regressions for quotas and bounded plugin-owned resources."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from plugins.adnmb import main as adnmb_main
from plugins.adnmb.adapi import AdnmbClient
from plugins.chat import main as chat_main
from plugins.url_parser import main as url_parser_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader


def _chat_context(
    *,
    data_dir: Path,
    user_limit: int   = 1,
    global_limit: int = 10,
) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            config={
                "timezone": "Asia/Shanghai",
                "plugins": {
                    "chat": {
                        "daily_user_limit": user_limit,
                        "daily_global_limit": global_limit,
                    }
                },
            },
            http_session = object(),
            data_dir     = data_dir,
            logger       = MagicMock(),
            secrets      = {
                "plugins": {
                    "chat": {
                        "token": "token",
                        "bot_id": "bot",
                    }
                }
            },
            state={},
        )
    )


@pytest.mark.asyncio
async def test_chat_quota_resets_at_business_date_boundary(monkeypatch, tmp_path):
    context = _chat_context(data_dir=tmp_path)
    current_date = ["2026-07-14"]
    calls        = 0

    async def fake_call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"messages": [{"type": "answer", "content": "ok"}]}

    monkeypatch.setattr(chat_main, "_business_date", lambda _context: current_date[0])
    monkeypatch.setattr(chat_main, "call_coze_api", fake_call)

    first           = await chat_main.handle("chat", "first", {"user_id": 7}, context)
    denied          = await chat_main.handle("chat", "second", {"user_id": 7}, context)
    current_date[0] = "2026-07-15"
    next_day        = await chat_main.handle("chat", "third", {"user_id": 7}, context)

    assert text_segments_text(first) == "ok"
    assert "额度已用完" in text_segments_text(denied)
    assert text_segments_text(next_day) == "ok"
    assert calls == 2
    assert chat_main.AtomicJsonStore(tmp_path / "chat_quota.json").read(None) == {
        "window": "2026-07-15",
        "users": {"7": 1},
        "total": 1,
    }


@pytest.mark.asyncio
async def test_chat_failed_remote_call_rolls_back_reserved_quota(monkeypatch, tmp_path):
    context = _chat_context(data_dir=tmp_path)
    responses = [None, {"messages": [{"type": "answer", "content": "retry-ok"}]}]

    async def fake_call(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(chat_main, "_business_date", lambda _context: "2026-07-14")
    monkeypatch.setattr(chat_main, "call_coze_api", fake_call)

    failed = await chat_main.handle("chat", "first", {"user_id": 8}, context)
    assert "对话失败" in text_segments_text(failed)
    assert chat_main.AtomicJsonStore(tmp_path / "chat_quota.json").read(None) == {
        "window": "2026-07-14",
        "users": {},
        "total": 0,
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

    monkeypatch.setattr(chat_main, "_business_date", lambda _context: "2026-07-14")
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


def test_url_preview_cache_is_scoped_to_stable_data_directory(tmp_path):
    context = SimpleNamespace(data_dir=tmp_path / "plugin-data")
    cache = url_parser_main._preview_cache(context)

    assert cache.directory == tmp_path / "plugin-data" / "url_previews"
    assert cache.limits == url_parser_main.PREVIEW_CACHE_LIMITS


class _SharedSession:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeAdnmbClient:
    created: ClassVar[list[_FakeAdnmbClient]] = []

    def __init__(self, session, cache_dir: Path, uuid: str = "") -> None:
        self.session     = session
        self.cache_dir   = cache_dir
        self.uuid        = uuid
        self.closed      = False
        self.close_calls = 0
        self.created.append(self)

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


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
async def test_adnmb_client_registry_evicts_lru_and_shutdown_closes_wrappers_only(
    monkeypatch, tmp_path
):
    _FakeAdnmbClient.created = []
    monkeypatch.setattr(adnmb_main, "AdnmbClient", _FakeAdnmbClient)
    monkeypatch.setattr(adnmb_main, "MAX_CACHED_CLIENTS", 2)
    context   = _adnmb_context(tmp_path)
    cache_dir = context.data_dir / "images"

    first = adnmb_main._get_client(context, cache_dir, user_id="1")
    second = adnmb_main._get_client(context, cache_dir, user_id="2")
    third = adnmb_main._get_client(context, cache_dir, user_id="3")

    assert first.closed is True
    assert second.closed is False
    assert third.closed is False
    assert len(context.state["adnmb_runtime"]["clients"]) == 2

    await adnmb_main.shutdown(context)

    assert second.closed is True
    assert third.closed is True
    assert context.http_session.close_calls == 0
    assert "adnmb_runtime" not in context.state


def test_adnmb_client_registry_expires_idle_entries(monkeypatch, tmp_path):
    _FakeAdnmbClient.created = []
    monkeypatch.setattr(adnmb_main, "AdnmbClient", _FakeAdnmbClient)
    monkeypatch.setattr(adnmb_main, "CLIENT_IDLE_TTL_SECONDS", 1)
    context   = _adnmb_context(tmp_path)
    cache_dir = context.data_dir / "images"

    first = adnmb_main._get_client(context, cache_dir, user_id="1")
    registry = context.state["adnmb_runtime"]["clients"]
    registry["client:1"].last_used = 0.0
    second = adnmb_main._get_client(context, cache_dir, user_id="2")

    assert first.closed is True
    assert second.closed is False
    assert tuple(registry) == ("client:2",)


@pytest.mark.asyncio
async def test_adnmb_forum_cache_has_ttl_capacity_and_copy_boundary(monkeypatch, tmp_path):
    client = AdnmbClient(session=object(), cache_dir=tmp_path)
    calls = 0

    async def fake_get(_endpoint: str, **_params):
        nonlocal calls
        calls += 1
        forums = [
            {"name": f"forum-{index}", "id": index} for index in range(client_module_limit + 5)
        ]
        return [{"forums": forums}]

    client_module_limit = 3
    monkeypatch.setattr("plugins.adnmb.adapi.MAX_FORUM_CACHE_ENTRIES", client_module_limit)
    monkeypatch.setattr(client, "_get", fake_get)

    first            = await client.get_forum_list()
    first["mutated"] = "outside"
    cached           = await client.get_forum_list()
    assert calls == 1
    assert len(cached) == client_module_limit
    assert "mutated" not in cached

    client._forum_cache_expires_at = 0.0
    await client.get_forum_list()
    assert calls == 2


def test_disabled_adnmb_user_module_is_removed_from_runtime_tree():
    root       = REPOSITORY_ROOT
    plugin_dir = root / "plugins" / "adnmb"

    assert not (plugin_dir / "user.py").exists()
    assert all(
        "from .user" not in path.read_text(encoding="utf-8")
        and "import .user" not in path.read_text(encoding="utf-8")
        for path in plugin_dir.glob("*.py")
    )
