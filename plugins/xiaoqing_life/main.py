"""小青的网上生活：入口、定时检查、后台上网任务与对聊天侧的服务回调。

定时回调必须在 Core 的 60 秒预算内返回：门控和决策同步完成，上网本身是登记过的后台任务
（单飞，``shutdown`` 时取消并等待）。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

from core.interfaces import PluginContextProtocol
from core.plugin_base import segments
from core.public_errors import public_error_response

from . import commands, persona_bridge
from . import turn_context as turn_context_mod
from .config import LifeConfig, load_life_config, route_configured
from .lifecycle import seed_interests
from .planner import REST, WIND_DOWN, Decision, day_key, decide
from .session import SessionDeps, run_session
from .sources.base import BoundedFetcher
from .stores import LifeStore, roll_day

logger = logging.getLogger(__name__)

_RUNTIME_KEY  = "xiaoqing_life.runtime"
_DISABLED_TIP = "网上生活没有开启：把 config.plugins.xiaoqing_life.enabled 设为 true 后再试。"


class LifeRuntime:
    """进程内状态：存储、唯一的上网任务、已登记的后台任务。"""

    def __init__(self, store: LifeStore) -> None:
        self.store                           = store
        self.rng                             = random.Random()
        self.task: asyncio.Task[None] | None = None
        self.closing                         = False
        self._bg: set[asyncio.Task[Any]]     = set()
        # 测试里可换成固定时钟。
        self.clock: Callable[[Any], datetime] = persona_bridge.now

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        """登记一个后台任务；关闭中不再接新任务。"""

        if self.closing:
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def shutdown(self) -> None:
        self.closing = True
        pending      = [t for t in (self.task, *self._bg) if t is not None and not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.store.flush()


def get_runtime(context: Any) -> LifeRuntime:
    state   = context.state
    runtime = state.get(_RUNTIME_KEY)
    if not isinstance(runtime, LifeRuntime):
        store = LifeStore(context.data_dir)
        store.load()
        runtime             = LifeRuntime(store)
        state[_RUNTIME_KEY] = runtime
    return runtime


async def init(context: PluginContextProtocol) -> None:
    get_runtime(context)


async def shutdown(context: PluginContextProtocol) -> None:
    runtime = context.state.get(_RUNTIME_KEY)
    if isinstance(runtime, LifeRuntime):
        await runtime.shutdown()


# ───────────────────────────── 上网任务 ─────────────────────────────


def _deps(context: Any, runtime: LifeRuntime) -> SessionDeps:
    async def complete(route: str, messages: list[dict[str, str]]) -> str:
        result = await context.capabilities.ai.complete(route, messages)
        return str(result.content)

    return SessionDeps(
        fetch        = BoundedFetcher(context.http_session),
        complete     = complete,
        rng          = runtime.rng,
        now          = lambda: runtime.clock(context),
        persona      = persona_bridge.persona_facts(context),
        reasoning_ok = route_configured(context, "reasoning"),
    )


async def _run_guarded(
    context: Any, runtime: LifeRuntime, cfg: LifeConfig, decision: Decision
) -> None:
    try:
        await run_session(runtime.store, cfg, decision, _deps(context, runtime))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("xiaoqing_life session crashed error_type=%s", type(exc).__name__)
        try:
            await runtime.store.mutate(
                lambda s: s.state.__setitem__("last_failure_at", runtime.clock(context).timestamp())
            )
        except Exception as inner:
            logger.warning(
                "xiaoqing_life failure record failed error_type=%s", type(inner).__name__
            )


def _decide(context: Any, runtime: LifeRuntime, cfg: LifeConfig, *, manual: bool) -> Decision:
    moment   = runtime.clock(context)
    store    = runtime.store
    presence = persona_bridge.availability(context, cfg, moment)
    roll_day(store.state, day_key(moment))
    seed_interests(store.interests, cfg.seed_interests, moment.timestamp())
    return decide(
        cfg,
        store.state,
        store.episodes,
        store.threads,
        store.interests,
        moment,
        presence,
        runtime.rng,
        running = runtime.running,
        manual  = manual,
    )


async def _tick(context: Any, *, manual: bool) -> Decision:
    cfg     = load_life_config(context)
    runtime = get_runtime(context)
    decision = _decide(context, runtime, cfg, manual=manual)
    if not cfg.enabled or runtime.closing:
        return decision
    if decision.choice in (REST, WIND_DOWN):
        # 睡前整理尚未接入：只记决策，不上网、不写经历。
        at = runtime.clock(context).timestamp()
        await runtime.store.mutate(
            lambda s: s.state.__setitem__("last_decision", decision.record(at))
        )
        logger.info(
            "xiaoqing_life step=decision choice=%s reason=%s", decision.choice, decision.reason
        )
        return decision
    # 同步登记任务，避免两次检查同时通过门控而重复出发；决策由会话开始时写入状态。
    logger.info("xiaoqing_life step=decision choice=%s reason=%s", decision.choice, decision.reason)
    task = runtime.spawn(_run_guarded(context, runtime, cfg, decision))
    if task is not None:
        runtime.task = task
    return decision


async def scheduled_tick(context: PluginContextProtocol) -> None:
    """每 15 分钟检查一次：门控与决策同步完成，上网交给后台任务。"""

    try:
        await _tick(context, manual=False)
    except Exception as exc:
        logger.warning("xiaoqing_life tick failed error_type=%s", type(exc).__name__)


# ───────────────────────────── 聊天侧服务 ─────────────────────────────


async def turn_context(
    payload: dict[str, Any], context: PluginContextProtocol
) -> dict[str, Any] | None:
    """每轮回复前的取用：纯内存选择，失败或关闭时返回 ``None``。"""

    try:
        cfg = load_life_config(context)
        if not cfg.enabled:
            return None
        runtime = get_runtime(context)
        response, dirty = turn_context_mod.turn_context(
            runtime.store, cfg, payload, runtime.clock(context), runtime.rng
        )
        if dirty:
            runtime.spawn(runtime.store.flush())
        return response
    except Exception as exc:
        logger.warning("xiaoqing_life turn_context failed error_type=%s", type(exc).__name__)
        return None


async def export_memes(
    payload: dict[str, Any], context: PluginContextProtocol
) -> dict[str, Any] | None:
    """梗图导出；梗图收集尚未接入，先返回空页让清单有效。"""

    try:
        if not load_life_config(context).enabled:
            return None
        since = payload.get("since") if isinstance(payload, dict) else 0.0
        since = (
            float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else 0.0
        )
        return {"memes": [], "revoked": [], "next_since": since}
    except Exception as exc:
        logger.warning("xiaoqing_life export_memes failed error_type=%s", type(exc).__name__)
        return None


# ───────────────────────────── /life ─────────────────────────────


async def _run_command(context: Any) -> str:
    cfg = load_life_config(context)
    if not cfg.enabled:
        return _DISABLED_TIP
    decision = await _tick(context, manual=True)
    if decision.choice in (REST, WIND_DOWN):
        return f"这会儿不上网：{decision.reason}"
    return "已安排一次上网，稍后用 /life recent 看看。"


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    del command, event
    try:
        action, rest = commands.route(args)
        runtime = get_runtime(context)
        cfg     = load_life_config(context)
        moment  = runtime.clock(context)
        store   = runtime.store
        if action == "help":
            return segments(commands.HELP_TEXT)
        if action == "status":
            presence = persona_bridge.availability(context, cfg, moment)
            return segments(commands.status_text(store, cfg, presence, moment))
        if action == "recent":
            return segments(commands.recent_text(store, moment))
        if action == "slang":
            return segments(commands.slang_text(store, moment))
        if action == "threads":
            return segments(commands.threads_text(store))
        if action in ("memes", "meme"):
            return segments("梗图收集还没有接入。")
        if action == "run":
            return segments(await _run_command(context))
        if action == "forget":
            return segments(await commands.forget_term(store, rest))
        return segments(f"未知子命令。\n\n{commands.HELP_TEXT}")
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="xiaoqing_life.handle")
