"""把剧本送进小青的两种方式，以及单轮回放。

- 进程内（inproc）：在本进程里直接调用插件的 ``observe_message`` → ``handle_smalltalk``，模型真实
  调用，发送模拟为成功回执。能逐轮记下审查候选、模型调用和步骤日志，适合看细节；一个剧本
  一个进程，插件的运行时状态互不干扰。
- 端到端（live）：用测试副本配置启动真实 ``main.py``，本进程扮演 NapCat（OneBot 正向 WS 服务端）
  推送事件、应答 ``send_*_msg`` 并分配消息 ID，走完 Core 的分发、投递回执和定时任务。数据目录、
  OneBot 地址和默认群都指向测试环境；``secrets.json`` 不动，运行用的是多一个测试管理员的副本，
  收尾时删掉。需要改行为配置的剧本临时改写 ``xiaoqing_config.json``，结束后逐字节恢复。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from core.clock import DEFAULT_CONFIG_TIMEZONE
from core.log import LogEvent

from .scenarios import BOT_ID, BOT_NAME, Suite, user_id

PROJECT_ROOT    = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG  = PROJECT_ROOT / "config" / "config.json"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.json"
BEHAVIOR_FILE   = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "config" / "xiaoqing_config.json"
PERSONA_FILE    = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "config" / "persona.md"
ZONE            = ZoneInfo(DEFAULT_CONFIG_TIMEZONE)
NAPCAT_PORT     = 11990
CHAT_LOGGER     = "plugins.xiaoqing_chat"
# 机器人日志里她主动开口的那一行：``<logger>: proactive.send <字段>``，事件名后面是空格或行尾。
_PROACTIVE_SEND_RE = re.compile(rb": proactive\.send(?=[ \r\n]|$)", re.MULTILINE)
# 评测用的时区；为空时用配置文件里的。小青按这个时区的钟点过作息，见 ``set_timezone``。
_timezone_override = ""
# 只有这几种准备需要改行为配置，端到端驱动把这些剧本放到最后逐个跑。
BEHAVIOR_SETUPS = frozenset({"busy_persona", "proactive"})
# 端到端驱动里发 /mem consolidate 的测试管理员（只写进这次运行的密钥副本）。
EVAL_ADMIN_ID = 864099


def set_timezone(name: str | None) -> None:
    """让这次评测里的小青按 ``name`` 时区的钟点过日子。

    评测跑在真实时钟上：上海的凌晨开跑，她按作息在睡觉，插话变少、回复犯困，分数没法和白天的
    比。换一个此刻是白天的时区（比如 ``UTC``），预置数据的日期和 Bot 配置一起跟着换。
    剧本里图书馆闭馆海报的日期例外：它在 ``scenarios`` 导入时就按上海时区定下，不跟着换。
    """

    global ZONE, _timezone_override
    if name:
        ZONE               = ZoneInfo(name)
        _timezone_override = name


def load_config() -> dict[str, Any]:
    """本机的 ``config.json`` 原样读出。"""

    return json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def load_secrets() -> dict[str, Any]:
    """本机的 ``secrets.json`` 原样读出。"""

    return json.loads(DEFAULT_SECRETS.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _eval_config() -> dict[str, Any]:
    """评测用的配置：沿用本机的模型和路由，去掉只属于部署的覆盖。

    部署配置优先于仓库里的默认值；评测要测的是仓库里的行为，所以小青只留模型路由，各插件的
    定时目标（``schedules``）也不带进来，剧本需要的行为由驱动自己设置。
    """

    config  = load_config()
    plugins = config.get("plugins")
    if isinstance(plugins, dict):
        for name, section in list(plugins.items()):
            if not isinstance(section, dict):
                continue
            section.pop("schedules", None)
            if name == "xiaoqing_chat":
                plugins[name] = {key: section[key] for key in ("ai",) if key in section}
    if _timezone_override:
        config["timezone"] = _timezone_override
    return config


def redact_secrets(text: str) -> str:
    """结果文件里不留凭据：Bearer 令牌和 key=value 形式的密钥换成占位。"""

    text = str(text or "")
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
        flags=re.I,
    )
    return text[:800]


def make_ai_service(
    session: Any, config: dict[str, Any], secrets: dict[str, Any], plugin_name: str
) -> Any:
    """按 ``plugin_name`` 的路由配置直连模型的 AI 能力，和 Core 交给插件的是同一种 ``AIService``。"""

    from core.ai import complete_configured_route, list_configured_models
    from core.capabilities import AIService

    async def complete(**kwargs: Any) -> Any:
        return await complete_configured_route(
            session=session, config=config, secrets=secrets, plugin_name=plugin_name, **kwargs
        )

    def list_models(**kwargs: Any) -> Any:
        return list_configured_models(
            config=config, secrets=secrets, plugin_name=plugin_name, **kwargs
        )

    return AIService(complete, list_models)


def make_plugin_context(
    *,
    session: Any,
    config: dict[str, Any],
    secrets: dict[str, Any],
    data_dir: Path,
    user_id: int,
    group_id: int | None,
    request_id: str,
    plugin_name: str = "xiaoqing_chat",
) -> Any:
    """进程内驱动用的插件上下文：模型真实调用，发送一律记为成功。

    和生产上下文一样，插件只拿得到 AI 路由能力，读不到统一密钥。
    """

    from core.delivery import receipt_from_action
    from core.interfaces import PluginCapabilities, PluginSettingsSnapshot

    async def send_action(action: dict[str, Any]) -> bool:
        receipt = receipt_from_action(action)
        if receipt is not None:
            await receipt.record(True)
        return True

    return SimpleNamespace(
        config=config,
        get_settings_snapshot=lambda: PluginSettingsSnapshot(config=config, secrets={}, revision=0),
        secrets={},
        capabilities=PluginCapabilities(ai=make_ai_service(session, config, secrets, plugin_name)),
        plugin_name      = plugin_name,
        plugin_dir       = PROJECT_ROOT / "plugins" / plugin_name,
        data_dir         = data_dir,
        http_session     = session,
        send_action      = send_action,
        current_user_id  = user_id,
        current_group_id = group_id,
        request_id       = request_id,
        state            = {},
    )


# ── 事件与记录 ──────────────────────────────────────────────────────────


def is_name_call(event: dict[str, Any]) -> bool:
    """和 Core 一样判断：只喊名字、只 @ 她（没带图）是在看她在不在。"""

    from core.message import extract_text, parse_text_command_context

    text = extract_text(event.get("message")) or ""
    return parse_text_command_context(
        text, event, bot_name=BOT_NAME, self_id=str(BOT_ID)
    ).is_only_bot_name


async def respond(chat: Any, raw: str, event: dict[str, Any], context: Any) -> Any:
    """按 Core 的分流调用聊天插件：只喊一声走短应答入口，其余走闲聊。"""

    if is_name_call(event):
        return await chat.call_bot_name_only(context)
    return await chat.handle_smalltalk(raw, event, context)


def build_event(
    item: dict[str, Any],
    *,
    group_id: int | None,
    message_id: int,
    fixtures: Path,
    last_bot_id: int | None,
    suite_index: int = 0,
) -> dict[str, Any]:
    """一条剧本消息对应的 OneBot 消息事件；``group_id`` 为 None 时是私聊。"""

    uid                         = user_id(item["speaker"], suite_index)
    shown_name                  = item.get("nick") or item["speaker"]
    parts: list[dict[str, Any]] = []
    if item["quote"] == "bot" and last_bot_id is not None:
        parts.append({"type": "reply", "data": {"id": str(last_bot_id)}})
    if item["at"] == "bot":
        parts.append({"type": "at", "data": {"qq": str(BOT_ID), "name": BOT_NAME}})
    elif item["at"]:
        parts.append(
            {
                "type": "at",
                "data": {"qq": str(user_id(item["at"], suite_index)), "name": item["at"]},
            }
        )
    if item["text"]:
        parts.append({"type": "text", "data": {"text": item["text"]}})
    if item["media"]:
        if item["media"] == "broken":
            encoded = "invalid-image-payload"
        else:
            encoded = base64.b64encode((fixtures / item["media"]).read_bytes()).decode("ascii")
        parts.append({"type": item["kind"], "data": {"file": "base64://" + encoded}})
    if item["face"]:
        parts.append({"type": "face", "data": {"id": item["face"]}})
    at_name = BOT_NAME if item["at"] == "bot" else item["at"]
    event   = {
        "post_type"  : "message",
        "time"       : int(time.time() - float(item.get("hours_ago") or 0.0) * 3600),
        "self_id"    : BOT_ID,
        "user_id"    : uid,
        "message_id" : message_id,
        "message"    : parts,
        "raw_message": (f"@{at_name} " if at_name else "") + item["text"],
        "font"       : 0,
    }
    if group_id is None:
        event.update(
            message_type = "private",
            sub_type     = "friend",
            sender       = {"user_id": uid, "nickname": shown_name},
        )
    else:
        event.update(
            message_type = "group",
            sub_type     = "normal",
            group_id     = group_id,
            message_seq  = message_id,
            sender       = {
                "user_id" : uid,
                "nickname": shown_name,
                "card"    : shown_name,
                "role"    : "member",
            },
        )
    return event


def new_row(run: str, suite: str, index: int, episode: int, item: dict[str, Any]) -> dict[str, Any]:
    keys = ("speaker", "text", "expect", "media", "kind", "face", "at", "quote", "tag", "chat")
    return {
        "run"    : run,
        "suite"  : suite,
        "index"  : index,
        "episode": episode,
        **{key: item[key] for key in keys},
        "nick"       : item.get("nick") or "",
        "forbid"     : item.get("forbid") or [],
        "expect_any" : item.get("expect_any") or [],
        "online"     : item.get("online") or "",
        "started_utc": datetime.now(UTC).isoformat(),
        "checks"     : [],
        "calls"      : [],
        "steps"      : [],
        "emissions"  : [],
        "errors"     : [],
    }


def append_row(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def segments_of(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return [segment for segment in message or () if isinstance(segment, dict)]


def bubble_text(segments: list[dict[str, Any]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    ).strip()


def _print_case(run_name: str, index: int, **fields: Any) -> None:
    """每轮结束打一行 JSON 进度，进程内驱动写进 ``<剧本>.log``。"""

    line = {"case": f"{run_name}.{index:02d}", **fields}
    print(json.dumps(line, ensure_ascii=False), flush=True)


def record_consolidate(
    output: Path, row: dict[str, Any], run_name: str, index: int, summary: str
) -> None:
    """整理轮不是消息，没有回复：记下整理结果，写一行记录。"""

    row["consolidate"]    = summary
    row["reply_segments"] = []
    append_row(output, row)
    _print_case(run_name, index, consolidate=summary[:200])


def record_turn(output: Path, row: dict[str, Any], run_name: str, index: int, shown: str) -> None:
    """一轮收尾：写一行记录，进度里带上这句话和她的回复（``shown``）。"""

    append_row(output, row)
    _print_case(run_name, index, **{"in": row["text"][:30], "out": shown[:80]})


def seed_data(
    chat_data: Path, setup: tuple[str, ...], *, group_id: int | None, suite_index: int = 0
) -> None:
    """开跑前预置的数据：昨天的生活、阿梨 5 小时前到期的考试。"""

    now = datetime.now(ZONE)
    if "seed_yesterday" in setup:
        days = chat_data / "life" / "days"
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        days.mkdir(parents=True, exist_ok=True)
        (days / f"{yesterday}.json").write_text(
            json.dumps(
                {
                    "day"         : yesterday,
                    "generated_at": now.timestamp() - 86400,
                    "events"      : [
                        {"at": "08:10", "text": "大物实验测的数据全歪了，约了下午重做"},
                        {"at": "12:20", "text": "食堂二楼糖醋里脊终于没卖完"},
                        {"at": "22:40", "text": "熄灯后补了两集芙莉莲"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    if "seed_exam_loop" in setup and group_id is not None:
        loops = chat_data / "life" / "open_loops"
        loops.mkdir(parents=True, exist_ok=True)
        (loops / f"g{group_id}.json").write_text(
            json.dumps(
                [
                    {
                        "loop_id"   : "seed0001",
                        "user_id"   : user_id("阿梨", suite_index),
                        "user_name" : "阿梨",
                        "title"     : "数电期末考试",
                        "due_at"    : now.timestamp() - 5 * 3600,
                        "created_at": now.timestamp() - 30 * 3600,
                        "status"    : "open",
                        "asked_at"  : 0.0,
                        "surfaced"  : 0,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def _seed_episode(item: dict[str, Any], index: int, *, at: float, day: str) -> dict[str, Any]:
    return {
        "id"          : f"seed{index:02d}",
        "at"          : at,
        "day"         : day,
        "activity"    : "hot_scroll",
        "source"      : item["source"],
        "item_id"     : f"bili:{900 + index}",
        "title"       : item["title"],
        "reaction"    : item["reaction"],
        "feeling"     : item["feeling"],
        "share"       : item["share"],
        "topics"      : item["topics"],
        "term_keys"   : [],
        "thread_id"   : "",
        "gist"        : item["gist"],
        "uploader"    : item["uploader"],
        "url"         : item["url"],
        "duration_s"  : 0,
        "dense_moment": "",
    }


def _seed_meme(life_dir: Path, approved_at: float) -> dict[str, Any]:
    """一张她在评论区收的、已经通过判定的反应梗图（画的是一只瞪大眼睛的猫）。"""

    from io import BytesIO

    from PIL import Image, ImageDraw

    image  = Image.new("RGB", (240, 240), (250, 244, 230))
    canvas = ImageDraw.Draw(image)
    canvas.ellipse((40, 60, 200, 210), fill=(70, 70, 70))
    canvas.polygon([(50, 90), (70, 30), (100, 70)], fill=(70, 70, 70))
    canvas.polygon([(190, 90), (170, 30), (140, 70)], fill=(70, 70, 70))
    for x in (85, 155):
        canvas.ellipse((x - 22, 105, x + 22, 150), fill=(255, 255, 255))
        canvas.ellipse((x - 8, 118, x + 8, 136), fill=(0, 0, 0))
    canvas.ellipse((108, 165, 132, 190), fill=(200, 80, 90))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = buffer.getvalue()
    sha     = hashlib.sha256(payload).hexdigest()
    (life_dir / "memes").mkdir(parents=True, exist_ok=True)
    (life_dir / "memes" / f"{sha}.png").write_bytes(payload)
    return {
        sha: {
            "sha256"       : sha,
            "ext"          : ".png",
            "label"        : "猫猫震惊",
            "description"  : "一只黑猫瞪圆眼睛张着嘴，一脸震惊",
            "visible_text" : "",
            "emotion_tags" : ["震惊", "无语"],
            "usage"        : "对方说了离谱或难以置信的事时发",
            "status"       : "ready",
            "created_at"   : approved_at,
            "approved_at"  : approved_at,
            "last_exported": 0.0,
        }
    }


def seed_online_life(life_dir: Path) -> None:
    """用确定的经历 / 词条 / 小课题 / 快照预置 xiaoqing_life 的隔离数据目录；时间戳相对现在。

    今天的经历均匀落在今天换日（她的一天从 ``DAY_ROLLOVER_HOUR`` 点算起）到现在之间，昨天的落在昨天
    的整点，所以“今天 / 昨天”的说法总是对得上；换日之前开跑时，从 0 点排起。
    """

    from plugins.xiaoqing_life.days import DAY_ROLLOVER_HOUR

    from .scenarios import ONLINE_LIFE_SEED as seed

    now = datetime.now(ZONE)
    today = now.replace(hour=0, minute=1, second=0, microsecond=0)
    if now.hour >= DAY_ROLLOVER_HOUR:
        today = today.replace(hour=DAY_ROLLOVER_HOUR)
    span                           = max(60.0, (now - today).total_seconds())
    episodes: list[dict[str, Any]] = []
    for n, item in enumerate(seed["today"]):
        at = today.timestamp() + span * (0.35 + 0.4 * n) * 0.9
        episodes.append(_seed_episode(item, n, at=at, day=now.strftime("%Y-%m-%d")))
    yesterday = now - timedelta(days=1)
    for n, item in enumerate(seed["yesterday"], start=len(episodes)):
        moment = yesterday.replace(hour=item["hour"], minute=20, second=0, microsecond=0)
        episodes.append(
            _seed_episode(item, n, at=moment.timestamp(), day=moment.strftime("%Y-%m-%d"))
        )
    term = seed["term"]
    key  = "".join(ch for ch in term["term"] if ch.isalnum())
    life_dir.mkdir(parents=True, exist_ok=True)

    def dump(name: str, value: Any) -> None:
        (life_dir / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    dump("episodes", episodes)
    dump("memes", _seed_meme(life_dir, now.timestamp() - 86400))
    dump(
        "terms",
        {
            key: {
                **term,
                "status"        : "known",
                "verified"      : True,
                "sources"       : ["baike:x", "bili:1"],
                "first_seen"    : now.timestamp() - 5 * 86400,
                "last_seen"     : now.timestamp() - 3600,
                "last_suggested": 0.0,
            }
        },
    )
    dump(
        "threads",
        [
            {
                "id"          : "tseed01",
                "title"       : seed["thread"]["title"],
                "why"         : seed["thread"]["why"],
                "next_query"  : seed["thread"]["next_query"],
                "status"      : "open",
                "steps"       : 1,
                "empty_streak": 0,
                "notes"       : [{"episode_id": "seed01", "note": "三脚架要稳"}],
                "takeaway"    : "",
                "created_at"  : now.timestamp() - 86400,
                "updated_at"  : now.timestamp() - 7200,
            }
        ],
    )
    dump(
        "snapshots",
        [
            {
                "topic"         : "天文摄影",
                "tier"          : 2,
                "stage"         : "repeated",
                "evidence_count": 3,
                "liked"         : 3,
                "since"         : now.timestamp() - 9 * 86400,
                "until"         : now.timestamp() - 3600,
                "created_at"    : now.timestamp() - 3600,
                "text"          : seed["snapshot"],
            }
        ],
    )


class LifeBridge:
    """把 xiaoqing_life 的服务回调接成聊天侧的 ``capabilities.life_context``（进程内直调）。"""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def turn_context(self, payload: dict[str, Any]) -> Any:
        from plugins.xiaoqing_life import main as life_main

        return await life_main.turn_context(dict(payload), self.context)

    async def export_memes(self, payload: dict[str, Any]) -> Any:
        from plugins.xiaoqing_life import main as life_main

        return await life_main.export_memes(dict(payload), self.context)


def make_life_bridge(config: dict[str, Any], life_dir: Path) -> LifeBridge:
    """隔离配置里启用 xiaoqing_life，路由沿用 xiaoqing_chat 的；数据目录就是预置过的那个。"""

    cfg                      = copy.deepcopy(config)
    plugins                  = cfg.setdefault("plugins", {})
    chat_ai                  = plugins.get("xiaoqing_chat", {}).get("ai", {})
    plugins["xiaoqing_life"] = {"enabled": True, "ai": chat_ai}
    snapshot = SimpleNamespace(config=cfg, secrets={})
    context = SimpleNamespace(
        data_dir              = life_dir,
        state                 = {},
        get_settings_snapshot = lambda: snapshot,
        http_session          = None,
        capabilities=SimpleNamespace(ai=None),
    )
    return LifeBridge(context)


class MemoryBridge:
    """统一记忆的进程内桥：和 Core 一样把调用方名字与主体范围写进载荷，回调直接调插件入口。"""

    def __init__(self, context: Any) -> None:
        self.context = context

    def service(self, caller: str, scope: str) -> Any:
        from core.capabilities import MEMORY_METHODS, MemoryService
        from plugins.xiaoqing_memory import main as memory_main

        def method(name: str) -> Any:
            async def invoke(payload: dict[str, Any]) -> Any:
                return await getattr(memory_main, name)(dict(payload), self.context)

            return invoke

        return MemoryService(
            caller,
            "user" if scope else "system",
            scope,
            {name: method(name) for name in MEMORY_METHODS},
        )

    async def consolidate(self) -> str:
        from plugins.xiaoqing_memory import main as memory_main

        runtime = memory_main.get_runtime(self.context)
        cfg     = memory_main.load_memory_config(self.context)
        return str(await memory_main.consolidate_now(self.context, runtime, cfg, ""))

    async def close(self) -> None:
        from plugins.xiaoqing_memory import main as memory_main

        await memory_main.shutdown(self.context)


def make_memory_bridge(
    session: Any, config: dict[str, Any], secrets: dict[str, Any], memory_dir: Path
) -> MemoryBridge:
    context = make_plugin_context(
        session     = session,
        config      = config,
        secrets     = secrets,
        data_dir    = memory_dir,
        user_id     = 0,
        group_id    = None,
        request_id  = "memory",
        plugin_name = "xiaoqing_memory",
    )
    return MemoryBridge(context)


def seed_memory(memory_dir: Path, suite_name: str, group_index: int, user_index: int = 0) -> None:
    """把剧本预置的旧对话写进统一记忆的事件日志（几天前说的），随后由剧本里的整理步骤处理。

    ``group_index`` 决定剧本的群号，``user_index`` 决定群友的 QQ 号（进程内驱动的群友号码不分剧本）。
    带 ``kind`` 的条目不是消息，是旧系统迁移来的记录（人物事实、她答应过的事），和上线那天
    xiaoqing_chat 的一次性导入走同一条路。
    """

    from plugins.xiaoqing_memory.ops import apply_record
    from plugins.xiaoqing_memory.scopes import Caller
    from plugins.xiaoqing_memory.store import MemoryStore

    from .scenarios import MEMORY_SEED

    items = MEMORY_SEED.get(suite_name) or []
    if not items:
        return
    store                                     = MemoryStore(memory_dir)
    caller                                    = Caller("xiaoqing_chat", "system", "")
    now                                       = time.time()
    by_scope: dict[str, list[dict[str, Any]]] = {}
    imports : dict[str, list[dict[str, Any]]] = {}
    for offset, item in enumerate(items):
        speaker = item["speaker"]
        if item["where"] in ("group", "group2"):
            # group2 是剧本第二段（换到的那个群）。
            scope = f"g{suite_group(group_index, 0 if item['where'] == 'group' else 1)}"
        else:
            owner = next(
                (
                    other["speaker"]
                    for other in items
                    if other["where"] == "private" and other["speaker"] != "bot"
                ),
                speaker,
            )
            scope = f"u{user_id(owner, user_index)}"
        ts = now - float(item["days"]) * 86400 + offset * 30
        if item.get("kind"):
            imports.setdefault(scope, []).append(
                _seed_import(item, f"seed:{suite_name}:{offset}", scope, ts, user_index)
            )
            continue
        if speaker == "bot":
            event = {
                "kind"  : "message",
                "ts"    : ts,
                "actor" : {"role": "assistant", "name": BOT_NAME},
                "origin": "self",
                "text"  : item["text"],
                "key"   : f"seed:{suite_name}:{offset}",
            }
        else:
            event = {
                "kind": "message",
                "ts": ts,
                "actor": {"role": "user", "user_id": user_id(speaker, user_index), "name": speaker},
                "origin": "user",
                "text": item["text"],
                "key": f"seed:{suite_name}:{offset}",
            }
        by_scope.setdefault(scope, []).append(event)
    for scope, events in by_scope.items():
        apply_record(store, {"scope": scope, "events": events}, caller, scope)
    for scope, ops in imports.items():
        apply_record(store, {"scope": scope, "ops": ops}, caller, scope)


def _seed_import(
    item: dict[str, Any], key: str, scope: str, ts: float, user_index: int
) -> dict[str, Any]:
    """预置里的一条旧记录，按 ``memory.legacy`` 导入时的格式。"""

    if item["kind"] == "self.promise":
        return {
            "op"         : "import",
            "kind"       : "self.promise",
            "key"        : key,
            "scope"      : scope,
            "text"       : item["text"],
            "data"       : {"counterpart": item["speaker"], "source": "chat"},
            "observed_at": ts,
            "expires_at" : ts + float(item.get("expires_days") or 7) * 86400,
            "status"     : "active",
        }
    return {
        "op"         : "import",
        "kind"       : item["kind"],
        "key"        : key,
        "subject"    : f"person:{user_id(item['speaker'], user_index)}",
        "text"       : item["text"],
        "data"       : {"name": item["speaker"]},
        "observed_at": ts,
    }


def turn_group(suite: Suite, item: dict[str, Any], group: int | None) -> int | None:
    """这一条在哪儿说：剧本的群，或者（``chat=private``）说话人和她的私聊。"""

    if item.get("chat") == "private":
        return None
    if item.get("chat") == "group":
        return group
    return None if suite.chat == "private" else group


def suite_group(suite_index: int, episode: int) -> int:
    return 978_000 + suite_index * 20 + episode


# ── 进程内驱动 ──────────────────────────────────────────────────────────


class _RowLogHandler(logging.Handler):
    """把聊天插件这一轮记下的 ``LogEvent`` 分类记进当前轮次：步骤、读图结果、告警。

    只收 ``current_row`` 指向本轮的记录：前几轮留下的后台任务带着它们自己那一轮的值，
    这时记的日志不算进本轮。
    """

    def __init__(
        self, row: dict[str, Any], current_row: contextvars.ContextVar[dict[str, Any] | None]
    ) -> None:
        super().__init__(logging.INFO)
        self.row         = row
        self.current_row = current_row

    def emit(self, record: logging.LogRecord) -> None:
        event = record.msg
        if not isinstance(event, LogEvent) or self.current_row.get() is not self.row:
            return
        entry = {"step": event.name, **event.fields}
        if event.name.startswith("media."):
            # 读图结果单独记下，排查回复和图上内容对不上时用。
            self.row.setdefault("media_logs", []).append(entry)
        elif record.levelno >= logging.WARNING:
            self.row["errors"].append({"level": record.levelname.lower(), **entry})
        else:
            self.row["steps"].append(entry)


async def run_inproc_suite(
    name: str, suite: Suite, suite_index: int, seed: int, out_dir: Path, online_life: bool = False
) -> Path:
    """在隔离数据目录里逐轮驱动一个剧本，返回逐轮记录的 jsonl 路径。"""

    import aiohttp

    from core.capabilities import AIService
    from core.delivery import DeliverySegments
    from core.interfaces import PluginCapabilities
    from plugins.xiaoqing_chat import main as chat
    from plugins.xiaoqing_chat import reply_generator
    from plugins.xiaoqing_chat.runtime_state import get_state, reset_global_state
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    random.seed(seed)
    run_name = f"{name}-{seed}"
    output   = out_dir / f"{run_name}.jsonl"
    output.touch(exist_ok=False)
    data_dir = out_dir / "isolated" / run_name
    data_dir.mkdir(parents=True, exist_ok=False)
    for episode in range(len(suite.episodes)):
        seed_data(data_dir, suite.setup, group_id=suite_group(suite_index, episode))
    config                         = _eval_config()
    secrets                        = load_secrets()
    online_life                    = online_life or "online_life" in suite.setup
    life_bridge: LifeBridge | None = None
    if online_life:
        life_dir = out_dir / "isolated" / f"{run_name}-xiaoqing_life"
        seed_online_life(life_dir)
        life_bridge = make_life_bridge(config, life_dir)
    memory_dir = out_dir / "isolated" / f"{run_name}-xiaoqing_memory"
    if "memory_seed" in suite.setup:
        seed_memory(memory_dir, name, suite_index)
    reset_global_state()
    state = get_state()
    bind_all_stores(state, data_dir)

    current_row: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
        "current_row", default=None
    )
    original_check = reply_generator.check_reply

    async def capture_check(**kwargs: Any):
        result = await original_check(**kwargs)
        row    = current_row.get()
        if row is not None:
            row["checks"].append({"candidate": kwargs.get("reply", ""), "result": asdict(result)})
        return result

    reply_generator.check_reply = capture_check
    # 和生产一样记 INFO 级的步骤日志；每轮由 ``_RowLogHandler`` 收进这一轮的记录。
    chat_logger       = logging.getLogger(CHAT_LOGGER)
    chat_logger_level = chat_logger.level
    chat_logger.setLevel(logging.INFO)
    contexts: list[Any] = []
    index               = 0
    async with aiohttp.ClientSession() as session:
        memory = make_memory_bridge(session, config, secrets, memory_dir)
        try:
            for episode, turns in enumerate(suite.episodes):
                suite_chat  = suite_group(suite_index, episode)
                last_bot_id = None
                for item in turns:
                    index += 1
                    row = new_row(run_name, name, index, episode, item)
                    if item["tag"] == "await_proactive":
                        # 定时检查只在端到端驱动里有。
                        row["reply_segments"] = []
                        append_row(output, row)
                        continue
                    if index == 1 and life_bridge is not None:
                        # 和生产上的定时同步一样，先把 xiaoqing_life 收的梗图导进表情库。
                        from plugins.xiaoqing_chat.chat_runtime import load_runtime
                        from plugins.xiaoqing_chat.life.online import sync_life_memes

                        sync_context = make_plugin_context(
                            session    = session,
                            config     = config,
                            secrets    = secrets,
                            data_dir   = data_dir,
                            user_id    = 0,
                            group_id   = None,
                            request_id = f"{run_name}.memes",
                        )
                        sync_context.capabilities = SimpleNamespace(
                            life_context=life_bridge, ai=None
                        )
                        row["memes_synced"] = await sync_life_memes(
                            sync_context, load_runtime(sync_context)
                        )
                        # 和生产上 xiaoqing_life 的定时任务一样，把预置的经历发布进统一记忆；
                        # 剧本开头有整理步骤时，她的自我页就会引用这些经历。
                        from plugins.xiaoqing_life import main as life_main
                        from plugins.xiaoqing_life import memory_bridge as life_memory

                        life_context              = life_bridge.context
                        life_context.capabilities = SimpleNamespace(
                            ai=None, memory=memory.service("xiaoqing_life", "")
                        )
                        row["life_published"] = await life_memory.publish(
                            life_context,
                            life_main.get_runtime(life_context).store,
                            life_main.load_life_config(life_context),
                            time.time(),
                        )
                    if item["tag"] == "consolidate":
                        # 先等聊天把已有消息推完，再整理一次（相当于过了一夜）。
                        await asyncio.sleep(2)
                        from plugins.xiaoqing_chat.memory.client import sync_all

                        await sync_all(
                            SimpleNamespace(
                                data_dir     = data_dir,
                                capabilities = SimpleNamespace(
                                    memory=memory.service("xiaoqing_chat", "")
                                ),
                            ),
                            state,
                            bot_name=BOT_NAME,
                        )
                        record_consolidate(output, row, run_name, index, await memory.consolidate())
                        continue
                    group = turn_group(suite, item, suite_chat)
                    if index > 1 and item["pace"]:
                        await asyncio.sleep(item["pace"])
                    message_id = (group or user_id(item["speaker"])) % 1_000_000 * 1000 + index
                    event      = build_event(
                        item,
                        group_id    = group,
                        message_id  = message_id,
                        fixtures    = out_dir / "fixtures",
                        last_bot_id = last_bot_id,
                    )
                    token   = current_row.set(row)
                    context = make_plugin_context(
                        session    = session,
                        config     = config,
                        secrets    = secrets,
                        data_dir   = data_dir,
                        user_id    = event["user_id"],
                        group_id   = group,
                        request_id = f"{run_name}.{index:02d}",
                    )
                    contexts.append(context)
                    actual_ai = context.capabilities.ai

                    async def capture_ai(
                        *, route_name: str, _actual=actual_ai, _row=row, **kwargs: Any
                    ):
                        start = time.monotonic()
                        try:
                            result = await _actual.complete(route_name, **kwargs)
                        except Exception as exc:
                            _row["calls"].append(
                                {
                                    "route"  : route_name,
                                    "seconds": round(time.monotonic() - start, 3),
                                    "error"  : type(exc).__name__,
                                }
                            )
                            raise
                        _row["calls"].append(
                            {
                                "route"  : route_name,
                                "model"  : result.model,
                                "seconds": round(time.monotonic() - start, 3),
                                "content": redact_secrets(result.content),
                            }
                        )
                        return result

                    context.capabilities = PluginCapabilities(
                        ai=AIService(
                            capture_ai,
                            lambda *, route_name, _actual=actual_ai, **kw: _actual.list_models(
                                route_name, **kw
                            ),
                        ),
                        life_context = life_bridge,
                        memory       = memory.service(
                            "xiaoqing_chat", f"g{group}" if group else f"u{event['user_id']}"
                        ),
                    )
                    started       = time.monotonic()
                    original_send = context.send_action

                    async def capture_send(
                        action, *, _send=original_send, _row=row, _start=started
                    ):
                        _row["emissions"].append(
                            {
                                "at_seconds": round(time.monotonic() - _start, 3),
                                "segments"  : action.get("params", {}).get("message", []),
                            }
                        )
                        return await _send(action)

                    context.send_action = capture_send
                    previous_gate = state.get_reply_gate_decision(f"g{group}") if group else None
                    raw = event["raw_message"]
                    row_logs = _RowLogHandler(row, current_row)
                    chat_logger.addHandler(row_logs)
                    try:
                        await chat.observe_message(raw, event, context)
                        result = await respond(chat, raw, event, context)
                        if result:
                            row["emissions"].append(
                                {
                                    "at_seconds": round(time.monotonic() - started, 3),
                                    "segments"  : list(result),
                                }
                            )
                        # 模拟发送成功：消息 ID 随回执交给聊天侧，和真实发送链路一样记进聊天记录。
                        bot_message_id = message_id + 500
                        receipt        = (
                            result.delivery_receipt
                            if isinstance(result, DeliverySegments)
                            else None
                        )
                        if receipt is not None:
                            receipt.add_message_id(bot_message_id)
                            await receipt.record(True)
                        sent = [
                            segment
                            for emission in row["emissions"]
                            for segment in emission["segments"]
                        ]
                        if sent:
                            last_bot_id = bot_message_id
                        row["reply_segments"] = sent
                    except Exception as exc:
                        row["exception"] = {
                            "type"   : type(exc).__name__,
                            "message": redact_secrets(str(exc))[:1000],
                        }
                        row["reply_segments"] = []
                    finally:
                        chat_logger.removeHandler(row_logs)
                    row["seconds"] = round(time.monotonic() - started, 3)
                    gate           = state.get_reply_gate_decision(f"g{group}") if group else None
                    row["gate"]    = (
                        asdict(gate) if gate is not None and gate is not previous_gate else None
                    )
                    current_row.reset(token)
                    record_turn(output, row, run_name, index, bubble_text(row["reply_segments"]))
        finally:
            if contexts:
                await chat.shutdown(contexts[-1])
            await memory.close()
            reply_generator.check_reply = original_check
            chat_logger.setLevel(chat_logger_level)
    return output


# ── 端到端驱动 ──────────────────────────────────────────────────────────


class FakeNapCat:
    """OneBot 正向 WS 服务端：接受 main.py 的连接，推送事件，应答动作并分配消息 ID。"""

    def __init__(self) -> None:
        from aiohttp import web

        self.web                       = web
        self.ws: Any                   = None
        self.connected                 = asyncio.Event()
        self.outbound: list[dict]      = []
        self.messages: dict[int, dict] = {}
        self.calls: dict[str, int]     = {}
        self._seq                      = 7_000_000
        self._runner: Any              = None

    async def start(self, port: int) -> None:
        app = self.web.Application()
        app.router.add_get("/ws", self._handler)
        self._runner = self.web.AppRunner(app)
        await self._runner.setup()
        await self.web.TCPSite(self._runner, "127.0.0.1", port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handler(self, request):
        from aiohttp import WSMsgType

        ws = self.web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        async for message in ws:
            if message.type is WSMsgType.TEXT:
                payload = json.loads(message.data)
                if isinstance(payload, dict) and "action" in payload:
                    await self._answer(ws, payload)
        return ws

    async def _answer(self, ws, payload: dict[str, Any]) -> None:
        action             = str(payload.get("action"))
        params             = payload.get("params") or {}
        self.calls[action] = self.calls.get(action, 0) + 1
        status, retcode = "ok", 0
        data: Any = None
        if action in {"send_group_msg", "send_private_msg", "send_msg"}:
            self._seq += 1
            group  = params.get("group_id")
            target = (
                ("group", int(group)) if group else ("private", int(str(params.get("user_id"))))
            )
            self.outbound.append(
                {
                    "t"         : time.time(),
                    "target"    : target,
                    "message_id": self._seq,
                    "segments"  : segments_of(params.get("message")),
                }
            )
            self.messages[self._seq] = {
                "message_id": self._seq,
                "message"   : params.get("message"),
                "sender"    : {"user_id": BOT_ID, "nickname": BOT_NAME},
                "time"      : int(time.time()),
            }
            data = {"message_id": self._seq}
        elif action == "get_login_info":
            data = {"user_id": BOT_ID, "nickname": BOT_NAME}
        elif action == "get_msg":
            data = self.messages.get(int(params.get("message_id") or 0))
            if data is None:
                status, retcode = "failed", 100
        elif action in {"get_group_member_info", "get_stranger_info"}:
            uid  = int(params.get("user_id") or 0)
            data = {"user_id": uid, "nickname": "群友", "card": "群友", "role": "member"}
        await ws.send_json(
            {"status": status, "retcode": retcode, "data": data, "echo": payload.get("echo")}
        )

    async def push(self, event: dict[str, Any]) -> None:
        self.messages[int(event["message_id"])] = {
            "message_id": event["message_id"],
            "message"   : event["message"],
            "sender"    : event["sender"],
            "time"      : event["time"],
        }
        await self.ws.send_json(event)

    async def collect(
        self, target: tuple[str, int], start: int, *, wait: float, settle: float
    ) -> list[dict[str, Any]]:
        """等这个会话的回复：第一条之后安静 ``settle`` 秒就算说完，最长等 ``wait`` 秒。"""

        deadline = time.time() + wait
        seen, last_seen = 0, None
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            got = [r for r in self.outbound[start:] if r["target"] == target]
            if len(got) != seen:
                seen, last_seen = len(got), time.time()
            if last_seen is not None and time.time() - last_seen >= settle:
                break
        return [r for r in self.outbound[start:] if r["target"] == target]


class BehaviorOverride:
    """临时改写行为配置，``restore`` 逐字节恢复并核对哈希。"""

    def __init__(self, backup_dir: Path) -> None:
        self.original = BEHAVIOR_FILE.read_bytes()
        self.digest   = hashlib.sha256(self.original).hexdigest()
        self.applied  = False
        (backup_dir / "xiaoqing_config.backup.json").write_bytes(self.original)

    def apply(self, **sections: dict[str, Any]) -> None:
        self.applied = True
        data         = json.loads(self.original.decode("utf-8"))
        for key, value in sections.items():
            data[key] = {**data.get(key, {}), **value}
        BEHAVIOR_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8", newline="\n"
        )
        # 插件按修改时间热加载，确保时间戳变化可见。
        time.sleep(1.1)

    def restore(self) -> None:
        # 没改过就不写回：评测跑着的时候有人改了行为配置，不能拿开跑时的快照盖掉。
        if not self.applied:
            return
        self.applied = False
        BEHAVIOR_FILE.write_bytes(self.original)
        if hashlib.sha256(BEHAVIOR_FILE.read_bytes()).hexdigest() != self.digest:
            raise RuntimeError(f"行为配置恢复后哈希不一致，备份在 {BEHAVIOR_FILE}")


def _busy_persona(out_dir: Path) -> Path:
    """全天“上课（忙）”的人设卡副本，看作息对插话概率和语气的影响。"""

    head, _, rest = PERSONA_FILE.read_text(encoding="utf-8").partition("## 作息〔作息〕")
    _, _, tail = rest.partition("## 深度对话时〔深度对话〕")
    path = out_dir / "persona_busy.md"
    path.write_text(
        head
        + "## 作息〔作息〕\n\n- 每天 00:00-23:59 上课（忙）\n\n## 深度对话时〔深度对话〕"
        + tail,
        encoding="utf-8",
    )
    return path


class LiveBot:
    """一次端到端运行：预置数据、启动 main.py、逐个剧本推送消息、收尾恢复。"""

    def __init__(self, out_dir: Path, suites: dict[str, Suite], indexes: dict[str, int]) -> None:
        self.out_dir                           = out_dir
        self.suites                            = suites
        self.indexes                           = indexes
        self.napcat                            = FakeNapCat()
        self.process: subprocess.Popen | None  = None
        self.behavior: BehaviorOverride | None = None
        self.data                              = out_dir / "data"
        # main.py --root 的运行目录：测试副本配置和密钥在它的 config/ 下。
        self.root                  = out_dir / "runtime"
        self._log: Any             = None
        self._mid                  = 5_000_000
        self._secrets: Path | None = None

    def _proactive_groups(self) -> list[int]:
        return [
            suite_group(self.indexes[name], 0)
            for name, suite in self.suites.items()
            if "proactive" in suite.setup
        ]

    def _write_config(self) -> Path:
        config = _eval_config()
        config.update(
            {
                "data_root"            : str(self.data),
                "logging"              : {"console": True, "file": False},
                "enable_ws_client"     : True,
                "onebot_ws_uri"        : f"ws://127.0.0.1:{NAPCAT_PORT}/ws",
                "enable_inbound_server": False,
                "onebot_http_base"     : "",
                # 只把主动开口剧本的群设为默认群：其他插件的定时推送也只会进这个群。
                "default_group_ids"      : self._proactive_groups(),
                "enable_plugin_schedules": True,
                "enable_plugin_watcher"  : False,
                "napcat_account"         : BOT_ID,
            }
        )
        path = self.root / "config" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _write_secrets(self) -> Path:
        """这次运行的密钥副本：多一个测试管理员，用来发 /mem consolidate；收尾时删掉。"""

        secrets                   = load_secrets()
        admins                    = [int(item) for item in secrets.get("admin_user_ids") or ()]
        secrets["admin_user_ids"] = [*admins, EVAL_ADMIN_ID]
        path                      = self.root / "config" / "secrets.json"
        path.write_text(json.dumps(secrets, ensure_ascii=False), encoding="utf-8")
        return path

    async def __aenter__(self) -> LiveBot:
        chat_data = self.data / "xiaoqing_chat"
        for name, suite in self.suites.items():
            for episode in range(len(suite.episodes)):
                seed_data(
                    chat_data,
                    suite.setup,
                    group_id    = suite_group(self.indexes[name], episode),
                    suite_index = self.indexes[name],
                )
            if "memory_seed" in suite.setup:
                seed_memory(
                    self.data / "xiaoqing_memory", name, self.indexes[name], self.indexes[name]
                )
        self._write_config()
        self.behavior = BehaviorOverride(self.out_dir)
        await self.napcat.start(NAPCAT_PORT)
        self._secrets = self._write_secrets()
        self._log     = (self.out_dir / "bot.log").open("wb")
        # 走 main.py 的同一个入口，运行目录换成放测试副本配置的目录。
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(PROJECT_ROOT / "main.py"), "--root", str(self.root)],
            cwd           = PROJECT_ROOT,
            stdout        = self._log,
            stderr        = subprocess.STDOUT,
            env           = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        try:
            await asyncio.wait_for(self.napcat.connected.wait(), timeout=180)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        # 插件全部加载完、上线通知发完再开始。
        await asyncio.sleep(15)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            if self.behavior is not None:
                self.behavior.restore()
        finally:
            if self.process is not None and self.process.poll() is None:
                if os.name == "nt":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self.process.send_signal(signal.SIGINT)
                try:
                    # 在线程里等：事件循环要继续应答 WS 关闭握手，否则 Bot 侧的关闭会超时。
                    await asyncio.to_thread(self.process.wait, 60)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            if self._log is not None:
                self._log.close()
            await self.napcat.stop()
            if self._secrets is not None:
                self._secrets.unlink(missing_ok=True)

    def _apply_setup(self, suite: Suite) -> None:
        sections: dict[str, dict[str, Any]] = {}
        if "busy_persona" in suite.setup:
            sections["personality"] = {"persona_file": str(_busy_persona(self.out_dir))}
        if "proactive" in suite.setup:
            sections["proactive"] = {
                "enabled"                 : True,
                "group_ids"               : self._proactive_groups(),
                "min_group_idle_minutes"  : 5,
                "max_group_dormant_hours" : 12,
                "min_interval_hours"      : 0,
                "max_per_group_per_day"   : 5,
                "active_hours_start"      : "00:00",
                "active_hours_end"        : "24:00",
                "retry_after_skip_minutes": 0,
            }
        if sections and self.behavior is not None:
            self.behavior.apply(**sections)

    async def run_suite(self, name: str) -> Path:
        suite    = self.suites[name]
        run_name = f"{name}-live"
        output   = self.out_dir / f"{run_name}.jsonl"
        output.touch(exist_ok=False)
        self._apply_setup(suite)
        index = 0
        try:
            for episode, turns in enumerate(suite.episodes):
                suite_chat  = suite_group(self.indexes[name], episode)
                last_bot_id = None
                for item in turns:
                    index += 1
                    row = new_row(run_name, name, index, episode, item)
                    if item["tag"] == "consolidate":
                        record_consolidate(output, row, run_name, index, await self._consolidate())
                        continue
                    group  = turn_group(suite, item, suite_chat)
                    target = (
                        ("group", group)
                        if group is not None
                        else ("private", user_id(item["speaker"], self.indexes[name]))
                    )
                    start   = len(self.napcat.outbound)
                    started = time.monotonic()
                    if item["tag"] == "await_proactive":
                        got = await self._await_proactive(
                            ("group", group or 0), wait=item["wait"] or 1500
                        )
                    else:
                        if index > 1 and item["pace"]:
                            await asyncio.sleep(item["pace"])
                        self._mid += 1
                        await self.napcat.push(
                            build_event(
                                item,
                                group_id    = group,
                                message_id  = self._mid,
                                fixtures    = self.out_dir / "fixtures",
                                last_bot_id = last_bot_id,
                                suite_index = self.indexes[name],
                            )
                        )
                        addressed = (
                            item["at"] == "bot" or group is None or item["expect"] == "reply"
                        )
                        wait = item["wait"] or (75.0 if addressed else 35.0)
                        got  = await self.napcat.collect(
                            target, start, wait=wait, settle=30.0 if wait > 120 else 8.0
                        )
                    if got:
                        last_bot_id = got[-1]["message_id"]
                    row["emissions"] = [
                        {
                            "at_seconds": round(
                                r["t"] - time.time() + (time.monotonic() - started), 3
                            ),
                            "segments": r["segments"],
                        }
                        for r in got
                    ]
                    row["reply_segments"] = [segment for r in got for segment in r["segments"]]
                    row["seconds"]        = round(time.monotonic() - started, 3)
                    shown                 = " / ".join(bubble_text(r["segments"]) for r in got)
                    record_turn(output, row, run_name, index, shown)
        finally:
            # 没改过行为配置时 ``restore`` 什么也不做。
            if self.behavior is not None:
                self.behavior.restore()
        return output

    def _proactive_sends(self) -> int:
        log = self.out_dir / "bot.log"
        return len(_PROACTIVE_SEND_RE.findall(log.read_bytes())) if log.is_file() else 0

    async def _await_proactive(
        self, target: tuple[str, int], *, wait: float
    ) -> list[dict[str, Any]]:
        """等她自己在定时检查里开口，返回她说的那条；到时间没开口返回空。

        其他插件的定时推送也会进这个群，不是她说的：以机器人日志里的 ``proactive.send`` 为准，
        只取这一步之后发出的消息。
        """

        deadline = time.time() + wait
        sent     = self._proactive_sends()
        mark     = len(self.napcat.outbound)
        while time.time() < deadline:
            await asyncio.sleep(2)
            if self._proactive_sends() > sent:
                return await self.napcat.collect(target, mark, wait=60, settle=5)
            mark = len(self.napcat.outbound)
        return []

    async def _consolidate(self) -> str:
        """测试管理员私聊发 /mem consolidate，等记忆插件整理完回话。"""

        await asyncio.sleep(3)
        self._mid += 1
        event = {
            "post_type"   : "message",
            "message_type": "private",
            "sub_type"    : "friend",
            "time"        : int(time.time()),
            "self_id"     : BOT_ID,
            "user_id"     : EVAL_ADMIN_ID,
            "message_id"  : self._mid,
            "message"     : [{"type": "text", "data": {"text": "/mem consolidate"}}],
            "raw_message" : "/mem consolidate",
            "font"        : 0,
            "sender"      : {"user_id": EVAL_ADMIN_ID, "nickname": "评测管理员"},
        }
        start    = len(self.napcat.outbound)
        deadline = time.time() + 900
        await self.napcat.push(event)
        texts: list[str] = []
        while time.time() < deadline:
            got = await self.napcat.collect(("private", EVAL_ADMIN_ID), start, wait=30, settle=2)
            texts = [bubble_text(r["segments"]) for r in got]
            # 几个剧本同时请求时，后到的会收到“上一次整理还没做完”：等第一次整理的结果。
            if any(text.startswith(("整理完了", "整理没做完")) for text in texts):
                break
        return " / ".join(texts)


async def run_live(
    out_dir: Path, suites: dict[str, Suite], indexes: dict[str, int], *, concurrency: int = 3
) -> list[Path]:
    """一个 main.py 进程跑完全部剧本：互不相干的并发跑，要改行为配置的放到最后逐个跑。"""

    plain = [
        name for name, suite in suites.items() if not BEHAVIOR_SETUPS.intersection(suite.setup)
    ]
    behavior          = [name for name in suites if name not in plain]
    paths: list[Path] = []
    async with LiveBot(out_dir, suites, indexes) as bot:
        gate = asyncio.Semaphore(concurrency)

        async def guarded(name: str) -> Path:
            async with gate:
                return await bot.run_suite(name)

        paths.extend(await asyncio.gather(*(guarded(name) for name in plain)))
        for name in behavior:
            paths.append(await bot.run_suite(name))
        (out_dir / "napcat_calls.json").write_text(
            json.dumps(bot.napcat.calls, indent=2), encoding="utf-8"
        )
    return paths


# ── 单轮回放 ────────────────────────────────────────────────────────────


async def replay(chat_data: Path, chat_id: str, speaker: str, text: str, *, at_bot: bool) -> None:
    """在一份聊天数据的副本上，把记录截到这句话之前重放这一轮，打印每一稿候选和审查理由。"""

    import aiohttp

    from core.delivery import DeliverySegments
    from plugins.xiaoqing_chat import main as chat
    from plugins.xiaoqing_chat import reply_generator
    from plugins.xiaoqing_chat.runtime_state import get_state, reset_global_state
    from plugins.xiaoqing_chat.store_binding import bind_all_stores

    work = Path(tempfile.mkdtemp(prefix="chat-eval-replay-"))
    try:
        data_dir = work / "xiaoqing_chat"
        shutil.copytree(chat_data, data_dir)
        history = data_dir / f"{chat_id}.json"
        messages = json.loads(history.read_text(encoding="utf-8"))
        cut = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("role") == "user"
                and any(text in str(part.get("text", "")) for part in message.get("parts", []))
            ),
            len(messages),
        )
        history.write_text(json.dumps(messages[:cut], ensure_ascii=False), encoding="utf-8")
        for backup in data_dir.glob(f"{chat_id}.json.bak"):
            backup.unlink()

        config  = _eval_config()
        secrets = load_secrets()
        reset_global_state()
        bind_all_stores(get_state(), data_dir)
        original = reply_generator.check_reply

        async def capture(**kwargs: Any):
            result = await original(**kwargs)
            print(
                json.dumps(
                    {"candidate": kwargs.get("reply"), "result": asdict(result)},
                    ensure_ascii = False,
                    indent       = 1,
                )
            )
            return result

        reply_generator.check_reply = capture
        group                       = int(chat_id[1:]) if chat_id.startswith("g") else None
        item                        = {
            "speaker"  : speaker,
            "text"     : text,
            "quote"    : "",
            "at"       : "bot" if at_bot else "",
            "media"    : "",
            "kind"     : "image",
            "face"     : "",
            "hours_ago": 0.0,
        }
        event = build_event(
            item, group_id=group, message_id=99_000_001, fixtures=work, last_bot_id=None
        )
        try:
            async with aiohttp.ClientSession() as session:
                context = make_plugin_context(
                    session    = session,
                    config     = config,
                    secrets    = secrets,
                    data_dir   = data_dir,
                    user_id    = event["user_id"],
                    group_id   = group,
                    request_id = "replay",
                )
                await chat.observe_message(event["raw_message"], event, context)
                result = await respond(chat, event["raw_message"], event, context)
                print(
                    "最终发出：",
                    bubble_text(list(result)) if isinstance(result, DeliverySegments) else result,
                )
                await chat.shutdown(context)
        finally:
            reply_generator.check_reply = original
    finally:
        shutil.rmtree(work, ignore_errors=True)
