"""已经到达、还没开始处理的消息，按发送者分开登记。

Core 按“群 + 用户”串行处理事件：一条消息处理期间，同一个人后发的消息排在队里，处理它的插件
看不到。两个入站通道在把事件放进队列时登记，事件开始处理时注销；插件经 ``inbound_backlog``
能力只能查询当前事件的发送者自己在本会话里排着的消息，用来判断“对方还在接着说”。

登记只是提示，不参与调度：通道丢弃事件或停止服务时不会注销，超过保留时间的登记自动失效。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# 排队超过这么久的登记不再算“还在等”：通道丢弃的事件不会来注销。
_WAITING_TTL_SECONDS = 60.0
_MAX_PER_SENDER      = 20
_MAX_SENDERS         = 2048
# 交给插件的字段：够判断是不是接着说的聊天消息，不带鉴权和传输层信息。
_EXPOSED_KEYS = ("message_id", "message", "raw_message", "time")

_Sender = tuple[str, str]


def _sender_of(event: dict[str, Any]) -> tuple[_Sender, str] | None:
    """消息事件的（会话, 发送者）和消息 ID；其他事件不登记。"""

    if event.get("post_type") != "message":
        return None
    user_id, message_id = event.get("user_id"), event.get("message_id")
    if user_id is None or message_id is None or isinstance(message_id, bool):
        return None
    return (str(event.get("group_id") or ""), str(user_id)), str(message_id)


@dataclass
class InboundBacklog:
    """入站通道与事件处理入口共用的登记表；只在事件循环线程里读写。"""

    clock   : Callable[[], float] = time.monotonic
    _waiting: dict[_Sender, dict[str, tuple[float, dict[str, Any]]]] = field(default_factory=dict)
    # 两个通道可能各送来一份同一条消息：已经开始处理的不再登记成“还在等”。
    _started: dict[tuple[_Sender, str], float] = field(default_factory=dict)

    def arrived(self, event: dict[str, Any]) -> None:
        """通道把事件放进队列时调用。"""

        located = _sender_of(event)
        if located is None:
            return
        sender, message_id = located
        now = self.clock()
        self._prune(now)
        if (sender, message_id) in self._started:
            return
        if sender not in self._waiting and len(self._waiting) >= _MAX_SENDERS:
            return
        waiting = self._waiting.setdefault(sender, {})
        if message_id in waiting or len(waiting) >= _MAX_PER_SENDER:
            return
        waiting[message_id] = (
            now,
            {key: event[key] for key in _EXPOSED_KEYS if key in event},
        )

    def started(self, event: dict[str, Any]) -> None:
        """事件开始处理时调用：它不再算排队中的消息。"""

        located = _sender_of(event)
        if located is None:
            return
        sender, message_id = located
        self._started[(sender, message_id)] = self.clock()
        waiting                             = self._waiting.get(sender)
        if waiting is not None:
            waiting.pop(message_id, None)
            if not waiting:
                del self._waiting[sender]

    def waiting(self, user_id: Any, group_id: Any) -> tuple[dict[str, Any], ...]:
        """这位发送者在这个会话里排着、还没开始处理的消息，按到达顺序。"""

        if user_id is None:
            return ()
        now     = self.clock()
        waiting = self._waiting.get((str(group_id or ""), str(user_id)), {})
        return tuple(
            dict(event) for at, event in waiting.values() if now - at < _WAITING_TTL_SECONDS
        )

    def _prune(self, now: float) -> None:
        for key in [key for key, at in self._started.items() if now - at >= _WAITING_TTL_SECONDS]:
            del self._started[key]
        for sender in list(self._waiting):
            waiting = self._waiting[sender]
            for message_id in [
                message_id
                for message_id, (at, _event) in waiting.items()
                if now - at >= _WAITING_TTL_SECONDS
            ]:
                del waiting[message_id]
            if not waiting:
                del self._waiting[sender]
