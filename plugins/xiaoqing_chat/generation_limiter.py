"""按全局、会话、用户和日配额限制并发生成请求。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


class GenerationLimitExceeded(RuntimeError):
    """生成请求超过配置硬限制。"""


# 排队等会话生成槽时，被同会话更新的请求顶替。
CHAT_SUPERSEDED = "chat_superseded"

# 上一轮的回复交给 Core 后最多等这么久结算，略长于 Core 默认的 OneBot 回执超时（15 秒），
# 正常情况下回执都在这之内结算。再晚到的回执由规划快照的轮次号挡住，不会覆盖后面的轮次。
DELIVERY_SETTLE_WAIT_SECONDS = 20.0


@dataclass
class GenerationLimiter:
    _lock                 : asyncio.Lock = field(default_factory=asyncio.Lock)
    _global_inflight      : int = 0
    _chat_inflight        : dict[str, int] = field(default_factory=dict)
    _user_inflight        : dict[str, int] = field(default_factory=dict)
    _user_calls           : dict[str, deque[float]] = field(default_factory=dict)
    _chat_waiters         : dict[str, object] = field(default_factory=dict)
    _delivery_holds       : dict[str, asyncio.Event] = field(default_factory=dict)
    max_tracked_users     : int   = 10_000
    sweep_interval_seconds: float = 300.0
    _last_sweep_at        : float = 0.0

    def __post_init__(self) -> None:
        self._slot_released = asyncio.Condition(self._lock)

    async def _await_chat_slot(self, chat_id: str, max_per_chat: int, wait_seconds: float) -> None:
        """会话生成槽被占用时排队，只保留最新的一位等待者；调用方持有锁。

        旧一轮看到会话里有了更新的消息会在下一个检查点自行放弃，新一轮接着生成。
        直接拒绝新一轮的话，旧一轮随后作废，新消息又没被处理，整段对话都没人接。
        """

        # 新请求进场，之前还在排队的请求都让位。
        if self._chat_waiters.pop(chat_id, None) is not None:
            self._slot_released.notify_all()
        if self._chat_inflight.get(chat_id, 0) < max_per_chat:
            return
        if wait_seconds <= 0:
            raise GenerationLimitExceeded("chat_inflight")
        ticket                      = object()
        self._chat_waiters[chat_id] = ticket
        loop                        = asyncio.get_running_loop()
        deadline                    = loop.time() + wait_seconds
        try:
            while True:
                if self._chat_waiters.get(chat_id) is not ticket:
                    raise GenerationLimitExceeded(CHAT_SUPERSEDED)
                if self._chat_inflight.get(chat_id, 0) < max_per_chat:
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise GenerationLimitExceeded("chat_inflight")
                try:
                    await asyncio.wait_for(self._slot_released.wait(), remaining)
                except TimeoutError:
                    continue
        finally:
            if self._chat_waiters.get(chat_id) is ticket:
                del self._chat_waiters[chat_id]

    def hold_for_delivery(self, chat_id: str) -> Callable[[], None]:
        """登记一条已交出、还没结算的回复，返回结算后调用的释放函数。

        回复的最后一个气泡随事件返回给 Core 发送，生成槽在那之前就已释放；回执提交时才
        把回复写进聊天记录和规划状态。排在后面的一轮要等这次结算，否则它读到的历史里
        没有这条回复，规划器不知道小青刚说过话，接着又说一遍。
        """

        settled                       = asyncio.Event()
        self._delivery_holds[chat_id] = settled

        def release() -> None:
            settled.set()
            if self._delivery_holds.get(chat_id) is settled:
                del self._delivery_holds[chat_id]

        return release

    async def _await_delivery_settled(self, chat_id: str) -> None:
        settled = self._delivery_holds.get(chat_id)
        if settled is None or settled.is_set():
            return
        try:
            await asyncio.wait_for(settled.wait(), DELIVERY_SETTLE_WAIT_SECONDS)
        except TimeoutError:
            # 回执一直没结算：丢掉这个登记，后面的轮次不再为它等待。
            if self._delivery_holds.get(chat_id) is settled:
                del self._delivery_holds[chat_id]

    def _sweep_user_calls(self, cutoff: float) -> None:
        for tracked_user, tracked_calls in tuple(self._user_calls.items()):
            while tracked_calls and tracked_calls[0] < cutoff:
                tracked_calls.popleft()
            if not tracked_calls:
                self._user_calls.pop(tracked_user, None)

    @asynccontextmanager
    async def admit(
        self,
        *,
        chat_id: str,
        user_id: str,
        max_global: int,
        max_per_chat: int,
        max_per_user: int,
        max_calls_per_user_per_day: int,
        chat_wait_seconds: float = 0.0,
    ) -> AsyncIterator[None]:
        async with self._lock:
            if max_per_chat > 0:
                await self._await_chat_slot(chat_id, max_per_chat, chat_wait_seconds)
            # 先保留会话顺序，等待回执时仅占用本会话槽。
            self._chat_inflight[chat_id] = self._chat_inflight.get(chat_id, 0) + 1
        admitted = False
        try:
            await self._await_delivery_settled(chat_id)
            async with self._lock:
                # 回执结算后按最新配额准入，日调用次数从实际进入生成时登记。
                now    = time.time()
                cutoff = now - 86400.0
                if now - self._last_sweep_at >= max(0.0, self.sweep_interval_seconds) or len(
                    self._user_calls
                ) >= max(1, self.max_tracked_users):
                    self._sweep_user_calls(cutoff)
                    self._last_sweep_at = now
                if user_id not in self._user_calls and len(self._user_calls) >= max(
                    1, self.max_tracked_users
                ):
                    raise GenerationLimitExceeded("daily_user_capacity")
                calls = self._user_calls.setdefault(user_id, deque())
                while calls and calls[0] < cutoff:
                    calls.popleft()
                if max_global > 0 and self._global_inflight >= max_global:
                    raise GenerationLimitExceeded("global_inflight")
                if max_per_user > 0 and self._user_inflight.get(user_id, 0) >= max_per_user:
                    raise GenerationLimitExceeded("user_inflight")
                if max_calls_per_user_per_day > 0 and len(calls) >= max_calls_per_user_per_day:
                    raise GenerationLimitExceeded("daily_user_calls")
                self._global_inflight += 1
                self._user_inflight[user_id] = self._user_inflight.get(user_id, 0) + 1
                calls.append(now)
                admitted = True
            yield
        finally:
            async with self._lock:
                if admitted:
                    self._global_inflight = max(0, self._global_inflight - 1)
                    self._decrement(self._user_inflight, user_id)
                self._decrement(self._chat_inflight, chat_id)
                self._slot_released.notify_all()

    @staticmethod
    def _decrement(values: dict[str, int], key: str) -> None:
        remaining = values.get(key, 0) - 1
        if remaining > 0:
            values[key] = remaining
        else:
            values.pop(key, None)
