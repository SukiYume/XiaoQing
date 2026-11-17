"""小青的统一记忆：入口、四个服务回调、定时整理与 ``/mem`` 命令。

服务回调都在 Core 签发的上下文里运行：调用方名字和用户主体的范围由 Core 写进载荷。插件关闭或出错时
回调返回 ``None``，调用方按没有长期记忆处理。定时回调只做门控，整理在登记的后台任务里进行
（单飞，``shutdown`` 时取消并等待）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Coroutine, Mapping
from typing import Any
from zoneinfo import ZoneInfo

from core.clock import now_in_configured_timezone
from core.interfaces import PluginContextProtocol
from core.log import LogEvent
from core.plugin_base import build_action, resolve_bot_name, segments
from core.public_errors import public_error_message, public_error_response

from . import commands
from .config import MemoryConfig, load_memory_config, route_configured
from .consolidate.runner import (
    Counters,
    Deps,
    consolidate_scope,
    consolidate_self,
    nightly_pass,
    rewrite_page,
)
from .forget import forget as forget_records
from .model import GLOBAL
from .ops import apply_record
from .recall import recall as recall_blocks
from .scopes import SELF_SUBJECT, ScopeError, caller_of, request_scope, valid_scope
from .search import search as search_records
from .store import MemoryStore

log = logging.getLogger(__name__)

_RUNTIME_KEY  = "xiaoqing_memory.runtime"
_DISABLED_TIP = "统一记忆没有开启：把 config.plugins.xiaoqing_memory.enabled 设为 true 后再试。"


class MemoryRuntime:
    """进程内状态：存储、唯一的整理任务、已登记的后台任务。"""

    def __init__(self, store: MemoryStore) -> None:
        self.store                          = store
        self.task: asyncio.Task[Any] | None = None
        self.closing                        = False
        self._bg: set[asyncio.Task[Any]]    = set()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        if self.closing:
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def shutdown(self) -> None:
        self.closing = True
        pending = [task for task in (self.task, *self._bg) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.to_thread(self.store.flush)


def get_runtime(context: Any) -> MemoryRuntime:
    state   = context.state
    runtime = state.get(_RUNTIME_KEY)
    if not isinstance(runtime, MemoryRuntime):
        runtime             = MemoryRuntime(MemoryStore(context.data_dir))
        state[_RUNTIME_KEY] = runtime
    return runtime


async def init(context: PluginContextProtocol) -> None:
    get_runtime(context)


async def shutdown(context: PluginContextProtocol) -> None:
    runtime = context.state.get(_RUNTIME_KEY)
    if isinstance(runtime, MemoryRuntime):
        await runtime.shutdown()


def _bot_name(context: Any) -> str:
    try:
        name = context.get_settings_snapshot().config.get("bot_name")
    except Exception:
        name = None
    return resolve_bot_name(name)


def _zone(context: Any) -> ZoneInfo:
    moment = now_in_configured_timezone(context)
    zone   = moment.tzinfo
    return zone if isinstance(zone, ZoneInfo) else ZoneInfo("Asia/Shanghai")


def _today(context: Any) -> str:
    return now_in_configured_timezone(context).strftime("%Y-%m-%d")


def make_deps(context: Any) -> Deps:
    async def complete(messages: list[dict[str, str]]) -> str:
        result = await context.capabilities.ai.complete("reasoning", messages)
        return str(result.content)

    return Deps(complete=complete, bot_name=_bot_name(context), zone=_zone(context))


# ───────────────────────────── 服务回调 ─────────────────────────────


async def _serve(
    name: str,
    payload: Any,
    context: Any,
    work: Any,
) -> dict[str, Any] | None:
    try:
        cfg = load_memory_config(context)
        if not cfg.enabled:
            return None
        if not isinstance(payload, Mapping):
            return {"version": 1, "error": "payload"}
        caller = caller_of(payload)
        try:
            scope = request_scope(payload, caller)
        except ScopeError as exc:
            log.warning(
                LogEvent(
                    f"{name}.rejected",
                    caller      = caller.name or None,
                    error       = exc,
                    reason_code = str(exc),
                )
            )
            return {"version": 1, "error": str(exc)}
        runtime                = get_runtime(context)
        started                = time.monotonic()
        result: dict[str, Any] = await work(runtime, cfg, payload, caller, scope)
        elapsed                = time.monotonic() - started
        if elapsed > 0.2:
            log.info(
                LogEvent(f"{name}.slow", caller=caller.name or None, elapsed_ms=int(elapsed * 1000))
            )
        return result
    except Exception as exc:
        log.warning(LogEvent(f"{name}.failed", error=exc))
        return None


async def record(payload: dict[str, Any], context: PluginContextProtocol) -> dict[str, Any] | None:
    async def work(
        runtime: MemoryRuntime, cfg: MemoryConfig, data: Any, caller: Any, scope: str
    ) -> dict[str, Any]:
        async with runtime.store.locks.hold(f"write:{scope}"):
            result = await asyncio.to_thread(
                apply_record, runtime.store, data, caller, scope, task_keep_days=cfg.task_note_days
            )
        day = _today(context)
        runtime.store.bump(
            day,
            {
                "record.events"  : sum(1 for seq in result["seqs"] if seq),
                "record.notes"   : sum(1 for note_id in result["ids"] if note_id),
                "record.rejected": len(result["rejected"]),
            },
        )
        if result["rejected"]:
            log.info(
                LogEvent(
                    "record.rejected",
                    caller  = caller.name or None,
                    scope   = scope,
                    reasons = Counter(str(item["reason"]) for item in result["rejected"]),
                )
            )
        return result

    return await _serve("record", payload, context, work)


async def recall(payload: dict[str, Any], context: PluginContextProtocol) -> dict[str, Any] | None:
    async def work(
        runtime: MemoryRuntime, cfg: MemoryConfig, data: Any, caller: Any, scope: str
    ) -> dict[str, Any]:
        result = recall_blocks(runtime.store, cfg, data, caller=caller.name, scope=scope)
        day    = _today(context)
        blocks = result.get("blocks") or {}
        runtime.store.bump(
            day,
            {
                "recall.calls": 1,
                "recall.chars": sum(len(str(value)) for value in blocks.values()),
                "recall.lines": len(result.get("used") or ()),
                "recall.due"  : 1 if result.get("due") else 0,
            },
        )
        return result

    return await _serve("recall", payload, context, work)


async def search(payload: dict[str, Any], context: PluginContextProtocol) -> dict[str, Any] | None:
    async def work(
        runtime: MemoryRuntime, cfg: MemoryConfig, data: Any, caller: Any, scope: str
    ) -> dict[str, Any]:
        return search_records(runtime.store, data, scope=scope)

    return await _serve("search", payload, context, work)


async def forget(payload: dict[str, Any], context: PluginContextProtocol) -> dict[str, Any] | None:
    async def work(
        runtime: MemoryRuntime, cfg: MemoryConfig, data: Any, caller: Any, scope: str
    ) -> dict[str, Any]:
        async with runtime.store.locks.hold(f"write:{scope}"):
            result = await asyncio.to_thread(forget_records, runtime.store, data, scope=scope)
        log.info(
            LogEvent(
                "forget",
                caller     = caller.name or None,
                scope      = scope,
                removed    = result.get("removed"),
                generation = result.get("generation"),
            )
        )
        return result

    return await _serve("forget", payload, context, work)


# ───────────────────────────── 定时整理 ─────────────────────────────


async def _consolidate_guarded(context: Any, runtime: MemoryRuntime, cfg: MemoryConfig) -> None:
    try:
        await nightly_pass(
            runtime.store, cfg, make_deps(context), now_in_configured_timezone(context)
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(LogEvent("consolidate.failed", error=exc))


async def scheduled_tick(context: PluginContextProtocol) -> None:
    """每 30 分钟检查一次：落盘取用计数；该整理的范围交给后台任务。"""

    try:
        cfg     = load_memory_config(context)
        runtime = get_runtime(context)
        await asyncio.to_thread(runtime.store.flush)
        if not cfg.enabled or not route_configured(context) or runtime.running:
            return
        runtime.task = runtime.spawn(_consolidate_guarded(context, runtime, cfg))
    except Exception as exc:
        log.warning(LogEvent("tick.failed", error=exc))


# ───────────────────────────── /mem ─────────────────────────────


async def consolidate_now(
    context: Any, runtime: MemoryRuntime, cfg: MemoryConfig, scope: str
) -> str:
    if not route_configured(context):
        return "没有给 xiaoqing_memory 配置 reasoning 模型。"
    deps   = make_deps(context)
    scopes = [scope] if scope else [item for item in runtime.store.known_scopes() if item != GLOBAL]
    total  = Counters()
    for item in scopes:
        if item == GLOBAL:
            result = await consolidate_self(runtime.store, cfg, deps, force=True)
        else:
            result = await consolidate_scope(runtime.store, cfg, deps, item, force=True)
        total.merge(result)
    if not scope:
        total.merge(await consolidate_self(runtime.store, cfg, deps, force=True))
    runtime.store.bump(deps.day(), total, save=True)
    return "整理完了：" + (
        "，".join(f"{k}={v}" for k, v in sorted(total.items())) or "没有要整理的"
    )


async def _rebuild(context: Any, runtime: MemoryRuntime, cfg: MemoryConfig, scope: str) -> str:
    if not route_configured(context):
        return "没有给 xiaoqing_memory 配置 reasoning 模型。"
    deps     = make_deps(context)
    subjects = (
        sorted(
            {
                note.subject
                for note in runtime.store.notes(scope).values()
                if note.subject.startswith(("person:", "group:"))
            }
        )
        if scope != GLOBAL
        else [SELF_SUBJECT]
    )
    rewritten = 0
    for subject in subjects:
        result = await rewrite_page(runtime.store, cfg, deps, scope, subject, force=True)
        rewritten += result.get("pages.rewritten", 0)
    return f"{scope} 重写了 {rewritten}/{len(subjects)} 份页面。"


def _in_background(
    context: Any, runtime: MemoryRuntime, user_id: Any, work: Coroutine[Any, Any, str]
) -> list[dict[str, Any]]:
    """整理要调模型、可能超过命令的执行预算：放进后台，做完把结果私聊发给发起人。"""

    if runtime.running:
        work.close()
        return segments("上一次整理还没做完，等它做完再试。")

    async def run() -> None:
        try:
            text = await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = public_error_message(context, exc, component="xiaoqing_memory.background")
            text = f"整理没做完\n{failure}"
        action = build_action(segments(text), int(user_id), None) if user_id else None
        if action is not None:
            await context.send_action(action)

    runtime.task = runtime.spawn(run())
    return segments("开始整理了，做完把结果发给你。")


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    del command
    event_user = event.get("user_id") if isinstance(event, dict) else None
    try:
        action, rest = commands.route(args)
        cfg     = load_memory_config(context)
        runtime = get_runtime(context)
        store   = runtime.store
        zone    = _zone(context)
        day     = _today(context)
        parts   = rest.split()
        if action == "help":
            return segments(commands.HELP_TEXT)
        if action == "status":
            return segments(
                commands.status_text(
                    store, day=day, limit=cfg.max_llm_calls_per_day, enabled=cfg.enabled
                )
            )
        if action == "stats":
            return segments(commands.stats_text(store))
        if action == "page":
            if not parts:
                return segments("用法：/mem page <范围> [QQ号|group|self]")
            return segments(commands.page_text(store, parts[0], parts[1] if len(parts) > 1 else ""))
        if action == "search":
            if len(parts) < 2 or not valid_scope(parts[0]):
                return segments("用法：/mem search <范围> <关键词>")
            found = search_records(
                store,
                {
                    "query"           : " ".join(parts[1:]),
                    "include_events"  : True,
                    "include_inactive": True,
                    "limit"           : 12,
                },
                scope=parts[0],
            )
            rows = [
                f"{item.get('id')} [{item.get('kind', item.get('type'))}] {item.get('text')}"
                for item in found["items"]
            ]
            return segments("\n".join(rows) or "没找到。")
        if action == "why":
            return segments(
                commands.why_text(store, rest, zone) if rest else "用法：/mem why <笔记ID>"
            )
        if not cfg.enabled:
            return segments(_DISABLED_TIP)
        if action == "forget":
            if not parts or not valid_scope(parts[0]):
                return segments("用法：/mem forget <范围> [QQ号|笔记ID]")
            target                  = parts[1] if len(parts) > 1 else ""
            payload: dict[str, Any] = {"all": True}
            if target.isdigit():
                payload = {"user_id": int(target)}
            elif target:
                payload = {"ids": [target]}
            async with store.locks.hold(f"write:{parts[0]}"):
                result = await asyncio.to_thread(forget_records, store, payload, scope=parts[0])
            return segments(f"已处理：{result}")
        if action == "consolidate":
            scope = parts[0] if parts else ""
            if scope and not valid_scope(scope):
                return segments("范围写成 global、g<群号> 或 u<QQ号>。")
            return _in_background(
                context, runtime, event_user, consolidate_now(context, runtime, cfg, scope)
            )
        if action == "rebuild":
            if not parts or not valid_scope(parts[0]):
                return segments("用法：/mem rebuild <范围>")
            return _in_background(
                context, runtime, event_user, _rebuild(context, runtime, cfg, parts[0])
            )
        return segments(commands.HELP_TEXT)
    except Exception as exc:
        return public_error_response(context, exc, component="xiaoqing_memory.command")
