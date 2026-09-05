"""In-process delivery receipts for commit-after-ack plugin state."""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .interfaces import DeliveryTarget
from .message import validate_message_segments

logger = logging.getLogger(__name__)

DELIVERY_RECEIPT_KEY = "_delivery_receipt"
DeliveryCallback     = Callable[[], Awaitable[None] | None]


class DeliveryReceipt:
    """Resolve one logical reply only after every physical action is acknowledged."""

    def __init__(
        self,
        *,
        expected_actions: int,
        commit: DeliveryCallback,
        rollback: DeliveryCallback,
        unknown: DeliveryCallback,
    ) -> None:
        self._expected_actions  = max(1, int(expected_actions))
        self._delivered_actions = 0
        self._commit            = commit
        self._rollback          = rollback
        self._unknown           = unknown
        # add_expected_actions() 必须保持同步，因为拆分消息和计划任务会在开始
        # 发送前同步扩充物理 action 数。这里使用线程锁统一保护它与异步 record()
        # 的共享状态；临界区只做内存读写，绝不在持锁期间 await。
        self._lock                                 = threading.Lock()
        self._resolved                             = False
        self._committed                            = False
        self._outcome: bool | None                 = None
        self._callback_error: BaseException | None = None
        self._handoff_pending                      = False

    def defer_to_transport(self) -> None:
        """将收据结算交给 Core 或暂存队列拥有的实际传输边界。"""
        with self._lock:
            self._handoff_pending = True

    @property
    def handoff_pending(self) -> bool:
        """返回已登记的发送器是否仍拥有最终结算权。"""
        with self._lock:
            return self._handoff_pending and not self._resolved

    @property
    def resolved(self) -> bool:
        """是否已由未知、明确失败或全部成功物理 action 终结。"""

        with self._lock:
            return self._resolved

    @property
    def committed(self) -> bool:
        """提交回调是否成功完成；已 resolved 不等同于已 committed。"""

        with self._lock:
            return self._committed

    @property
    def outcome(self) -> bool | None:
        """返回最终三态结果；读取方须先检查 ``resolved``。"""

        with self._lock:
            return self._outcome

    @property
    def expected_actions(self) -> int:
        """返回当前逻辑回复需要确认的物理 action 总数。"""

        with self._lock:
            return self._expected_actions

    @property
    def callback_error(self) -> BaseException | None:
        """返回任一结算回调异常；投递结果本身不会因此被改写。"""

        with self._lock:
            return self._callback_error

    def add_expected_actions(self, count: int) -> None:
        """在开始投递前登记拆分产生的额外 action，避免过早提交状态。"""

        with self._lock:
            if self._resolved:
                return
            self._expected_actions += max(0, int(count))

    async def record(self, delivered: bool | None) -> None:
        """记录一次真实 transport ack，并至多执行一次提交或回滚回调。

        ``None`` 表示动作已经提交给传输层、但最终结果未知；它与明确拒绝分别
        调用 ``unknown`` 和 ``rollback``。只有全部物理 action 明确成功才提交。
        回调必须在锁外执行，否则异步提交会把 dispatcher 的后续 ack 一并阻塞。
        """

        callback: DeliveryCallback | None = None
        is_commit                         = False
        with self._lock:
            if self._resolved:
                return
            if delivered is None:
                self._resolved = True
                self._outcome  = None
                callback       = self._unknown
            elif delivered is False:
                self._resolved = True
                self._outcome  = False
                callback       = self._rollback
            elif delivered is True:
                self._delivered_actions += 1
                if self._delivered_actions >= self._expected_actions:
                    self._resolved = True
                    self._outcome  = True
                    callback       = self._commit
                    is_commit      = True
            else:
                raise TypeError("delivery outcome must be True, False, or None")
        if callback is None:
            return
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:
            with self._lock:
                self._callback_error = exc
            logger.exception("Delivery receipt callback failed")
        else:
            if is_commit:
                with self._lock:
                    self._committed = True


class DeliverySegments(list[dict[str, Any]]):
    """Message segments carrying an in-process receipt through the dispatcher."""

    def __init__(self, values: Sequence[dict[str, Any]], receipt: DeliveryReceipt) -> None:
        super().__init__(values)
        self.delivery_receipt = receipt


@dataclass(frozen=True, slots=True)
class ScheduledDelivery:
    """One target-specific scheduled message that Core validates and sends."""

    target: DeliveryTarget
    message: tuple[dict[str, Any], ...]
    receipt: DeliveryReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, DeliveryTarget):
            raise TypeError("scheduled delivery target must be a DeliveryTarget")
        normalized = validate_message_segments(list(self.message))
        if not normalized:
            raise ValueError("scheduled delivery message must not be empty")
        object.__setattr__(self, "message", tuple(normalized))

    @classmethod
    def group(
        cls,
        group_id: int,
        message: Sequence[dict[str, Any]],
        *,
        receipt: DeliveryReceipt | None = None,
    ) -> ScheduledDelivery:
        """Build a group delivery without exposing OneBot action construction."""

        return cls(DeliveryTarget("group", group_id), tuple(message), receipt)

    @classmethod
    def private(
        cls,
        user_id: int,
        message: Sequence[dict[str, Any]],
        *,
        receipt: DeliveryReceipt | None = None,
    ) -> ScheduledDelivery:
        """Build a private delivery without exposing OneBot action construction."""

        return cls(DeliveryTarget("private", user_id), tuple(message), receipt)


def receipt_from_action(action: dict[str, Any]) -> DeliveryReceipt | None:
    """读取进程内 receipt；外部伪造或序列化后的同名字段不会被执行。"""

    candidate = action.get(DELIVERY_RECEIPT_KEY)
    return candidate if isinstance(candidate, DeliveryReceipt) else None


def attach_receipt(action: dict[str, Any], receipt: DeliveryReceipt) -> dict[str, Any]:
    """把 receipt 绑定到仅在进程内流转的 action，并保留原 action 身份。"""

    action[DELIVERY_RECEIPT_KEY] = receipt
    return action


async def send_with_receipt(
    send_action: Callable[[dict[str, Any]], Awaitable[bool | None]],
    action: dict[str, Any],
    receipt: DeliveryReceipt,
) -> bool | None:
    """发送带收据的 action，并确保返回的三态结果恰好结算一次。"""

    # 异常由调用者按具体传输分类；超时可能表示结果未知，须保留其结算权。
    outcome = await send_action(attach_receipt(action, receipt))
    if not receipt.resolved and not receipt.handoff_pending:
        await receipt.record(outcome)
    return outcome


def strip_receipt(action: dict[str, Any]) -> dict[str, Any]:
    """复制并移除内部 receipt，防止 transport 序列化内部回调对象。"""

    return {key: value for key, value in action.items() if key != DELIVERY_RECEIPT_KEY}


async def resolve_action_handoff(action: dict[str, Any], *, delivered: bool) -> dict[str, Any]:
    """移除内部状态并记录一次真实 transport 结果。"""
    clean   = strip_receipt(action)
    receipt = receipt_from_action(action)
    if receipt is not None:
        await receipt.record(delivered)
    return clean
