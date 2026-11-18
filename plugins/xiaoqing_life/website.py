"""小青观测站。主程序和独立预览入口共用这套路由。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from core.clock import DEFAULT_CONFIG_TIMEZONE

from .config import SESSION_MINUTES, parse_life_config
from .days import day_key, life_date
from .stores import life_data_directory, read_collection
from .travel import travel_entries

SITE = Path(__file__).with_name("site")
DEFAULT_WEB_PORT = 12002
CHAT = SITE.parent.parent / "xiaoqing_chat"
PAGES = ("", "live", "diary", "timeline", "travel", "research", "gallery", "stats", "guide")
ACTIVITIES = {
    "hot_scroll"     : "翻翻热榜",
    "interest_dive"  : "找感兴趣的视频",
    "thread_continue": "查小课题的资料",
    "work_on"        : "整理自己的小作品",
    "term_lookup"    : "翻百科查词",
}


def live_status(root: Path, moment: datetime | None = None) -> dict:
    # 聊天插件可单独加载或重载，查询作息时使用它当前这一代的实现。
    from plugins.xiaoqing_chat.config.config import load_xiaoqing_chat_config
    from plugins.xiaoqing_chat.life.schedule import current_slot, parse_routine

    config = read_collection(root / "config/config.json", dict)
    zone   = ZoneInfo(config.get("timezone") or DEFAULT_CONFIG_TIMEZONE)
    now    = moment.astimezone(zone) if moment is not None else datetime.now(zone)
    cfg    = parse_life_config(config.get("plugins", {}).get("xiaoqing_life", {}))
    card = load_xiaoqing_chat_config(context_config=config, plugin_dir=CHAT).personality.card
    slots    = parse_routine(card.routine)
    slot     = current_slot(slots, now)
    activity = slot.activity if slot else "自由时间"
    scene    = "desk"
    if slot and slot.availability == "asleep":
        scene = "rest"
    elif any(word in activity for word in ("吃饭", "午饭", "晚饭", "早饭", "食堂")):
        scene = "meal"
    elif "天文社" in activity:
        scene = "club"
    elif "刷番" in activity:
        scene = "anime"
    elif "校园岔路" in activity:
        scene = "campus"
    elif "奶茶" in activity:
        scene = "tea"
    elif "掌机" in activity:
        scene = "game"
    elif "图书馆" in activity:
        scene = "library"
    elif slot and (slot.availability == "busy" or "上课" in activity):
        scene = "classroom"
    elif not slots:
        start, end = cfg.quiet_window()
        minute = now.hour * 60 + now.minute
        if start <= minute < end if start <= end else minute >= start or minute < end:
            scene, activity = "rest", "睡觉"
    state    = read_collection(life_data_directory(root, config) / "state.json", dict)
    current  = state.get("current") or {}
    recorded = cfg.enabled and current.get("start", 0) <= now.timestamp() < current.get("end", 0)
    if recorded and current.get("choice") in ACTIVITIES:
        scene, activity = "desk", ACTIVITIES[current["choice"]]
    else:
        recorded = False
    return {
        "at"      : now.isoformat(timespec="seconds"),
        "zone"    : zone.key,
        "scene"   : scene,
        "activity": activity,
        "source"  : "生活记录" if recorded else "日常作息",
        "routine" : list(card.routine),
    }


def archive(root: Path, moment: datetime | None = None) -> dict:
    config    = read_collection(root / "config/config.json", dict)
    zone      = ZoneInfo(config.get("timezone") or DEFAULT_CONFIG_TIMEZONE)
    now       = moment.astimezone(zone) if moment is not None else datetime.now(zone)
    directory = life_data_directory(root, config)

    def past(collection: str, key: str):
        rows = read_collection(directory / f"{collection}.json", list)
        return sorted(
            (
                r
                for r in rows
                if isinstance(r, dict)
                and isinstance(r.get(key), (int, float))
                and 0 < r[key] <= now.timestamp()
            ),
            key     = lambda r: r[key],
            reverse = True,
        )

    journal = [
        {"date": r.get("day") or r.get("week"), "kind": r["kind"], "text": r["text"]}
        for r in past("journal", "at")
        if r.get("kind") in ("diary", "weekly")
        and r.get("text")
        and (r.get("day") or r.get("week"))
    ]
    episodes = [
        {
            "at"      : datetime.fromtimestamp(r["at"], zone).isoformat(timespec="minutes"),
            "title"   : r["title"],
            "reaction": r.get("reaction") or r.get("gist") or "",
        }
        for r in past("episodes", "at")
        if r.get("share") and r.get("title")
    ]
    learnings = [
        {"topic": r["topic"], "text": r["text"], "verified": r.get("confidence") == "verified"}
        for r in past("learnings", "created_at")
        if r.get("topic") and r.get("text")
    ]
    threads = [
        {"title": r["title"], "status": r["status"]}
        for r in past("threads", "updated_at")
        if r.get("title") and r.get("status")
    ]
    stats         = read_collection(directory / "state.json", dict).get("stats", {})
    public_counts = (
        "episodes",
        "terms_seen",
        "thread_continue.rounds",
        "thread_continue.findings",
        "term_lookup.hits",
        "term_lookup.misses",
        *(f"session.{key}" for key in SESSION_MINUTES),
    )
    if not isinstance(stats, dict):
        raise ValueError("invalid daily counts")
    week = []
    for days_ago in range(6, -1, -1):
        day = (life_date(now) - timedelta(days=days_ago)).isoformat()
        row = stats.get(day, {})
        if not isinstance(row, dict):
            raise ValueError("invalid daily counts")
        counts = {key: row.get(key, 0) for key in public_counts}
        if not all(type(n) in (int, float) and n >= 0 and n % 1 == 0 for n in counts.values()):
            raise ValueError("invalid daily count")
        activities = {choice: counts[f"session.{choice}"] for choice in SESSION_MINUTES}
        week.append(
            {
                "day"       : day,
                "recorded"  : day in stats,
                "episodes"  : counts["episodes"],
                "sessions"  : sum(activities.values()),
                "activities": activities,
                "rounds"    : counts["thread_continue.rounds"],
                "findings"  : counts["thread_continue.findings"],
                "terms"     : counts["terms_seen"],
                "hits"      : counts["term_lookup.hits"],
                "misses"    : counts["term_lookup.misses"],
            }
        )
    return {
        "journal"  : journal,
        "episodes" : episodes,
        "threads"  : threads,
        "learnings": learnings,
        "week"     : week,
        "today"    : day_key(now),
        "zone"     : zone.key,
    }


def create_app(root: Path) -> web.Application:
    app = web.Application()

    async def page(request: web.Request) -> web.FileResponse:
        return web.FileResponse(SITE / "index.html")

    readers: dict[str, Callable[[Path], Any]] = {
        "/api/live"   : live_status,
        "/api/archive": archive,
        "/api/travel" : travel_entries,
    }

    async def api(request: web.Request) -> web.Response:
        try:
            data = await asyncio.to_thread(readers[request.path], root)
        except (ImportError, OSError, ValueError, KeyError, TypeError):
            return web.json_response({"error": "生活记录暂时读不到。"}, status=503)
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def travel_photo(request: web.Request) -> web.FileResponse:
        directory = life_data_directory(root) / "travel" / request.match_info["date"]
        path      = directory / (request.match_info["number"] + ".webp")
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    for name in PAGES:
        app.router.add_get(f"/{name}", page)
    app.router.add_get(r"/diary/{date:\d{4}-(?:\d{2}-\d{2}|W\d{2})}", page)
    for path in readers:
        app.router.add_get(path, api)
    app.router.add_get(r"/travel-media/{date:\d{4}-\d{2}-\d{2}}/{number:[12]}.webp", travel_photo)
    app.router.add_static("/assets/", SITE / "assets")
    return app


def web_port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("plugins.xiaoqing_life.web_port 需要填写 1 到 65535 之间的整数")
    return value


def main() -> None:
    from main import _default_root

    parser = argparse.ArgumentParser(description="小青观测站")
    parser.add_argument("--root", type=Path, default=_default_root(), help="小青的运行目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, help=f"覆盖 plugins.xiaoqing_life.web_port（默认 {DEFAULT_WEB_PORT}）"
    )
    args = parser.parse_args()
    root = args.root.resolve()
    port = args.port
    if port is None:
        config  = read_collection(root / "config/config.json", dict)
        options = config.get("plugins", {}).get("xiaoqing_life", {})
        port    = options.get("web_port", DEFAULT_WEB_PORT)
    try:
        port = web_port(port)
    except ValueError as exc:
        parser.error(str(exc))
    web.run_app(create_app(root), host=args.host, port=port)


if __name__ == "__main__":
    main()
