"""xiaoqing_life 测试共用的假 HTTP、假模型、假上下文与固定样例。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from plugins.xiaoqing_life.config import LifeConfig, parse_life_config
from plugins.xiaoqing_life.persona_bridge import Presence
from plugins.xiaoqing_life.sources.base import SourceError
from tests.helpers.settings_snapshot import settings_snapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int = 0, day: int = 5) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=SHANGHAI)


FREE = Presence("free", "", None)


class FakeFetcher:
    """按 URL 片段匹配的假取材；值可以是载荷、异常或接收参数的函数。"""

    def __init__(self, routes: Mapping[str, Any]) -> None:
        self.routes                                  = dict(routes)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> Any:
        self.calls.append((url, dict(params or {})))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value(params) if callable(value) else value
        raise SourceError("fake", "no_route")

    async def get_xml(
        self, url: str, *, params: Mapping[str, str] | None = None, referer: str = ""
    ) -> bytes:
        value = await self.get_json(url, params=params, referer=referer)
        return value if isinstance(value, bytes) else str(value).encode("utf-8")


def bili_video(aid: int, title: str, desc: str = "简介", **extra: Any) -> dict[str, Any]:
    return {
        "aid"  : aid,
        "bvid" : f"BV{aid}",
        "title": title,
        "desc" : desc,
        "owner": {"name": "up"},
        "tname": "科技",
        "stat" : {"view": 1000 + aid, "like": 10},
        **extra,
    }


def popular_payload(*videos: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "data": {"list": list(videos)}}


def comments_payload(*replies: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "data": {"replies": list(replies)}}


def bili_reply(
    text: str, like: int = 5, pictures: tuple[str, ...] = (), sub: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "content": {
            "message" : text,
            "pictures": [{"img_src": url} for url in pictures],
        },
        "like"   : like,
        "replies": [{"content": {"message": s}} for s in sub],
    }


class FakeAI:
    """记录调用的假 ``capabilities.ai``；回复按队列给出。"""

    def __init__(self, *replies: Any) -> None:
        self.replies                                       = list(replies)
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def complete(self, route: str, messages: list[dict[str, Any]], **_: Any) -> Any:
        self.calls.append((route, messages))
        reply = self.replies.pop(0) if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply if isinstance(reply, str) else json.dumps(reply))


def make_cfg(**options: Any) -> LifeConfig:
    return parse_life_config({"enabled": True, **options})


def make_context(
    tmp_path: Path,
    *,
    life: Mapping[str, Any] | None = None,
    ai: FakeAI | None              = None,
    session: Any                   = None,
) -> SimpleNamespace:
    """带完整 ai route 配置的假插件上下文。"""

    options = {
        "enabled": True,
        "ai"     : {"routes": {"reasoning": {"models": ["m"]}, "vision": {"models": ["m"]}}},
        **(life or {}),
    }
    config = {
        "timezone": "Asia/Shanghai",
        "bot_name": "小青",
        "plugins" : {"xiaoqing_life": options},
    }
    context = SimpleNamespace(
        data_dir     = tmp_path / "data",
        plugin_dir   = tmp_path / "plugin",
        state        = {},
        logger       = MagicMock(),
        http_session = session,
        request_id   = "req-1",
        capabilities=SimpleNamespace(ai=ai or FakeAI()),
        config=config,
    )
    context.get_settings_snapshot = lambda: settings_snapshot(config=context.config)
    return context


def fixed_clock(moment: datetime) -> Callable[[Any], datetime]:
    return lambda _context: moment
