"""把剧本送进小青的两种方式，以及单轮回放。

- 进程内（inproc）：在本进程里直接调用插件的 ``observe_message`` → ``handle_smalltalk``，模型真实
  调用，发送模拟为成功回执。能逐轮记下审查候选、模型调用和步骤日志，适合看细节；一个剧本
  一个进程，插件的运行时状态互不干扰。
- 端到端（live）：用测试副本配置启动真实 ``main.py``，本进程扮演 NapCat（OneBot 正向 WS 服务端）
  推送事件、应答 ``send_*_msg`` 并分配消息 ID，走完 Core 的分发、投递回执和定时任务。数据目录、
  OneBot 地址和默认群都指向测试环境，``secrets.json`` 只读；需要改行为配置的剧本临时改写
  ``xiaoqing_config.json``，结束后逐字节恢复。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .scenarios import BOT_ID, BOT_NAME, Suite, user_id

PROJECT_ROOT    = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG  = PROJECT_ROOT / "config" / "config.json"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.json"
BEHAVIOR_FILE   = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "config" / "xiaoqing_config.json"
PERSONA_FILE    = PROJECT_ROOT / "plugins" / "xiaoqing_chat" / "config" / "persona.md"
ZONE            = ZoneInfo("Asia/Shanghai")
NAPCAT_PORT     = 11990
# 只有这几种准备需要改行为配置，端到端驱动把这些剧本放到最后逐个跑。
BEHAVIOR_SETUPS = frozenset({"busy_persona", "proactive"})


# ── 事件与记录 ──────────────────────────────────────────────────────────


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
            sender       = {"user_id": uid, "nickname": item["speaker"]},
        )
    else:
        event.update(
            message_type = "group",
            sub_type     = "normal",
            group_id     = group_id,
            message_seq  = message_id,
            sender       = {
                "user_id" : uid,
                "nickname": item["speaker"],
                "card"    : item["speaker"],
                "role"    : "member",
            },
        )
    return event


def new_row(run: str, suite: str, index: int, episode: int, item: dict[str, Any]) -> dict[str, Any]:
    keys = ("speaker", "text", "expect", "media", "kind", "face", "at", "quote", "tag")
    return {
        "run"    : run,
        "suite"  : suite,
        "index"  : index,
        "episode": episode,
        **{key: item[key] for key in keys},
        "forbid"     : item.get("forbid") or [],
        "expect_any" : item.get("expect_any") or [],
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


def suite_group(suite_index: int, episode: int) -> int:
    return 978_000 + suite_index * 20 + episode


# ── 进程内驱动 ──────────────────────────────────────────────────────────


class _RowLogger:
    """把插件的 step 日志和告警记进当前轮次。"""

    def __init__(self, row: dict[str, Any], redact) -> None:
        self.row    = row
        self.redact = redact

    def _write(self, level: str, message: Any, *args: Any, **_kwargs: Any) -> None:
        rendered = str(message) % args if args else str(message)
        if "step=" in rendered:
            try:
                self.row["steps"].append(json.loads(rendered.split("step=", 1)[1]))
            except (ValueError, TypeError):
                self.row["steps"].append({"log": self.redact(rendered)[:2000]})
        elif " media={" in rendered:
            # 读图结果单独记下，排查回复和图上内容对不上时用。
            try:
                self.row.setdefault("media_logs", []).append(
                    json.loads(rendered.split(" media=", 1)[1])
                )
            except (ValueError, TypeError):
                pass
        elif level in {"warning", "error", "exception"}:
            self.row["errors"].append({"level": level, "message": self.redact(rendered)[:1000]})

    def debug(self, *a: Any, **k: Any) -> None:
        self._write("debug", *a, **k)

    def info(self, *a: Any, **k: Any) -> None:
        self._write("info", *a, **k)

    def warning(self, *a: Any, **k: Any) -> None:
        self._write("warning", *a, **k)

    def error(self, *a: Any, **k: Any) -> None:
        self._write("error", *a, **k)

    def exception(self, *a: Any, **k: Any) -> None:
        self._write("exception", *a, **k)


async def run_inproc_suite(
    name: str, suite: Suite, suite_index: int, seed: int, out_dir: Path
) -> Path:
    """在隔离数据目录里逐轮驱动一个剧本，返回逐轮记录的 jsonl 路径。"""

    import aiohttp

    from core.capabilities import AIService
    from core.delivery import DeliverySegments
    from core.interfaces import PluginCapabilities
    from plugins.xiaoqing_chat import main as chat
    from plugins.xiaoqing_chat import reply_generator
    from plugins.xiaoqing_chat.experiments.anthropomorphic_group import (
        make_experiment_context,
        redact_secrets,
    )
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
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    secrets = json.loads(DEFAULT_SECRETS.read_text(encoding="utf-8"))
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
    contexts: list[Any]         = []
    index                       = 0
    async with aiohttp.ClientSession() as session:
        try:
            for episode, turns in enumerate(suite.episodes):
                group       = None if suite.chat == "private" else suite_group(suite_index, episode)
                last_bot_id = None
                for item in turns:
                    index += 1
                    row = new_row(run_name, name, index, episode, item)
                    if item["tag"] == "await_proactive":
                        # 定时检查只在端到端驱动里有。
                        row["reply_segments"] = []
                        append_row(output, row)
                        continue
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
                    context = make_experiment_context(
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
                        )
                    )
                    context.logger = _RowLogger(row, redact_secrets)
                    started        = time.monotonic()
                    original_send  = context.send_action

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
                    try:
                        await chat.observe_message(raw, event, context)
                        result = await chat.handle_smalltalk(raw, event, context)
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
                    row["seconds"] = round(time.monotonic() - started, 3)
                    gate           = state.get_reply_gate_decision(f"g{group}") if group else None
                    row["gate"]    = (
                        asdict(gate) if gate is not None and gate is not previous_gate else None
                    )
                    append_row(output, row)
                    current_row.reset(token)
                    print(
                        json.dumps(
                            {
                                "case": f"{run_name}.{index:02d}",
                                "in"  : item["text"][:30],
                                "out" : bubble_text(row["reply_segments"])[:60],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
        finally:
            if contexts:
                await chat.shutdown(contexts[-1])
            reply_generator.check_reply = original_check
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
        (backup_dir / "xiaoqing_config.backup.json").write_bytes(self.original)

    def apply(self, **sections: dict[str, Any]) -> None:
        data = json.loads(self.original.decode("utf-8"))
        for key, value in sections.items():
            data[key] = {**data.get(key, {}), **value}
        BEHAVIOR_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8", newline="\n"
        )
        # 插件按修改时间热加载，确保时间戳变化可见。
        time.sleep(1.1)

    def restore(self) -> None:
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
        self._log: Any                         = None
        self._mid                              = 5_000_000

    def _proactive_groups(self) -> list[int]:
        return [
            suite_group(self.indexes[name], 0)
            for name, suite in self.suites.items()
            if "proactive" in suite.setup
        ]

    def _write_config(self) -> Path:
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        config.update(
            {
                "data_root"            : str(self.data),
                "log_to_file"          : False,
                "log_to_console"       : True,
                "log_use_color"        : False,
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
        path = self.out_dir / "config.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
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
        config_path   = self._write_config()
        self.behavior = BehaviorOverride(self.out_dir)
        await self.napcat.start(NAPCAT_PORT)
        self._log = (self.out_dir / "bot.log").open("wb")
        # 走 main.py 的同一个入口，只把配置文件换成测试副本。
        launcher = (
            "import sys, core.app as app\n"
            "real = app.ConfigManager\n"
            f"app.ConfigManager = lambda config, secrets: real({str(config_path)!r}, secrets)\n"
            "import main\n"
            "main.cli([])\n"
        )
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", launcher],
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
                group = (
                    None if suite.chat == "private" else suite_group(self.indexes[name], episode)
                )
                last_bot_id = None
                for item in turns:
                    index += 1
                    row    = new_row(run_name, name, index, episode, item)
                    target = (
                        ("group", group)
                        if group is not None
                        else ("private", user_id(item["speaker"], self.indexes[name]))
                    )
                    start   = len(self.napcat.outbound)
                    started = time.monotonic()
                    if item["tag"] == "await_proactive":
                        target = ("group", group or 0)
                        got    = await self.napcat.collect(
                            target, start, wait=item["wait"] or 1500, settle=5
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
                    append_row(output, row)
                    print(
                        json.dumps(
                            {
                                "case": f"{run_name}.{index:02d}",
                                "in"  : item["text"][:30],
                                "out" : " / ".join(bubble_text(r["segments"]) for r in got)[:80],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
        finally:
            if (
                self.behavior is not None
                and suite.setup
                and BEHAVIOR_SETUPS.intersection(suite.setup)
            ):
                self.behavior.restore()
        return output


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
    from plugins.xiaoqing_chat.experiments.anthropomorphic_group import make_experiment_context
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

        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        secrets = json.loads(DEFAULT_SECRETS.read_text(encoding="utf-8"))
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
                context = make_experiment_context(
                    session    = session,
                    config     = config,
                    secrets    = secrets,
                    data_dir   = data_dir,
                    user_id    = event["user_id"],
                    group_id   = group,
                    request_id = "replay",
                )
                await chat.observe_message(event["raw_message"], event, context)
                result = await chat.handle_smalltalk(event["raw_message"], event, context)
                print(
                    "最终发出：",
                    bubble_text(list(result)) if isinstance(result, DeliverySegments) else result,
                )
                await chat.shutdown(context)
        finally:
            reply_generator.check_reply = original
    finally:
        shutil.rmtree(work, ignore_errors=True)
