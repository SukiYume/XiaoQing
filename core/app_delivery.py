# mypy: disable-error-code=attr-defined
"""OneBot action delivery and inbound event collection."""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from collections.abc import Mapping
from typing import Any, cast

from .app_identity import AppIdentityService
from .app_support import _parse_group_ids, current_action_sink
from .constants import (
    DEFAULT_BOT_NAME,
    INBOUND_EVENT_DEDUP_TTL_SECONDS,
    MAX_INBOUND_EVENT_DEDUP_KEYS,
    MESSAGE_SPLIT_DELAY,
)
from .delivery import (
    DELIVERY_RECEIPT_KEY,
    DeliveryReceipt,
    attach_receipt,
    receipt_from_action,
)
from .dispatcher import Dispatcher
from .interfaces import ACTION_BYPASS_SINK_KEY, ACTION_RESULT_MESSAGE_ID_KEY
from .onebot import (
    OneBotActionOutcomeUnknown,
    OneBotHttpSender,
    OneBotWsClient,
)
from .plugin_base import build_action, segments, split_message_segments
from .plugin_execution import (
    PluginExecutionClosed,
    PluginExecutionTimeout,
    PluginExecutionUnavailable,
    call_plugin_callback,
    invoke_loaded_plugin,
)
from .plugin_manager import PluginManager
from .server import BroadcastResult, InboundManager

logger = logging.getLogger(__name__)


def _message_length(message: Any) -> int:
    """Return the unbounded logical length used by delivery metrics and logs."""

    if isinstance(message, str):
        return len(message)
    if not isinstance(message, list):
        return 0
    length = 0
    for segment in message:
        if not isinstance(segment, Mapping):
            length += 1
            continue
        if segment.get("type") != "text":
            length += 1
            continue
        raw_data = segment.get("data")
        data = raw_data if isinstance(raw_data, Mapping) else {}
        text = data.get("text", "")
        length += len(text) if isinstance(text, str) else len(str(text))
    return length


class AppDeliveryMixin:
    dispatcher: Dispatcher
    http_sender: OneBotHttpSender | None
    inbound_manager: InboundManager | None
    plugin_manager: PluginManager
    ws_client: OneBotWsClient | None
    _last_connect_notification_ts: float
    _onebot_auth_generation: int
    identity_service: AppIdentityService
    _runtime_onebot_credentials_trusted: bool
    _runtime_onebot_token: str
    _stopping: bool
    _ws_client_auth_generation: int
    _ws_client_auth_quarantine: OneBotWsClient | None

    async def _on_ws_connected(self) -> None:
        """WebSocket 连接成功回调"""
        ws_client = self.ws_client
        if not ws_client:
            return
        # 获取 default 群列表
        default_groups = self.config.get("default_group_ids", [])
        if not default_groups:
            logger.info("No default groups configured, skipping connect notification")
            return
        try:
            parsed_groups = _parse_group_ids(default_groups)
        except (TypeError, ValueError) as exc:
            logger.error("Connect notification skipped: invalid default_group_ids: %s", exc)
            return

        # 发送上线通知（可通过 config 配置）
        connect_msg = self.config.get("connect_notification")
        if connect_msg is None:
            bot_name = (
                str(self.config.get("bot_name") or DEFAULT_BOT_NAME).strip() or DEFAULT_BOT_NAME
            )
            connect_msg = f"🟢 {bot_name}已上线~"
        if not connect_msg:
            return
        now = time.monotonic()
        min_interval = self._connect_notification_min_interval()
        if (
            min_interval > 0
            and self._last_connect_notification_ts > 0
            and now - self._last_connect_notification_ts < min_interval
        ):
            logger.info("Connect notification suppressed by min interval")
            return
        self._last_connect_notification_ts = now
        message = [{"type": "text", "data": {"text": connect_msg}}]
        for group_id in parsed_groups:
            action = {
                "action": "send_group_msg",
                "params": {
                    "group_id": int(group_id),
                    "message": message,
                },
            }
            await self._send_action(action)

    def _connect_notification_min_interval(self) -> float:
        try:
            return max(
                0.0,
                float(self.config.get("connect_notification_min_interval_seconds", 300)),
            )
        except (TypeError, ValueError):
            return 300.0

    async def _process_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """处理事件并返回 action（通用逻辑）"""
        if self._stopping:
            logger.debug("Dropping event while XiaoQing is stopping")
            return None
        result = await self.dispatcher.handle_event(event)
        receipt = getattr(result, "delivery_receipt", None)
        segs = segments(result)
        action = build_action(segs, event.get("user_id"), event.get("group_id"))
        if not isinstance(receipt, DeliveryReceipt):
            return cast(dict[str, Any] | None, action)
        if action is None:
            await receipt.record(False)
            return None
        return cast(dict[str, Any], attach_receipt(action, receipt))

    def _onebot_credentials_are_trusted(self) -> bool:
        """Return the application-level credential publication decision."""

        with self._runtime_auth_lock:
            return self._runtime_onebot_credentials_trusted

    def _ws_transport_is_trusted(self, client: Any) -> bool:
        """Reject stale, quarantined, or legacy holders before any network call."""

        if client is None:
            return False
        with self._runtime_auth_lock:
            if (
                not self._runtime_onebot_credentials_trusted
                or client is not self.ws_client
                or client is self._ws_client_auth_quarantine
            ):
                return False
            if getattr(client, "credentials_trusted", None) is not True:
                return False
            # holder 属性可能是可重入描述符；读取后必须再次确认它仍是当前发布对象。
            return bool(
                self._runtime_onebot_credentials_trusted
                and client is self.ws_client
                and client is not self._ws_client_auth_quarantine
            )

    def _http_transport_is_trusted(self, sender: Any) -> bool:
        """Require both the global source decision and an explicit holder flag."""

        if sender is None:
            return False
        with self._runtime_auth_lock:
            if (
                not self._runtime_onebot_credentials_trusted
                or sender is not self.http_sender
                or getattr(sender, "credentials_trusted", None) is not True
            ):
                return False
            # 外部 holder 的属性读取可以重入并撤下自身，避免继续解引用已失效对象。
            if not self._runtime_onebot_credentials_trusted or sender is not self.http_sender:
                return False
            if not str(getattr(sender, "http_base", "")).strip():
                return False
            return self._runtime_onebot_credentials_trusted and sender is self.http_sender

    async def _request_onebot_action(
        self,
        action_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Request a correlated OneBot response without using action sinks/broadcast."""

        if self._stopping:
            return None
        action = {"action": action_name, "params": dict(params)}
        ws_client = self.ws_client
        if (
            ws_client is not None
            and self._ws_transport_is_trusted(ws_client)
            and ws_client.connected()
        ):
            try:
                response = await ws_client.request_action(action)
            except OneBotActionOutcomeUnknown:
                logger.warning(
                    "OneBot WS action %s was committed but its outcome is unknown; "
                    "HTTP fallback is suppressed",
                    action_name,
                )
                return None
            if response is not None:
                return cast(dict[str, Any], response)
        http_sender = self.http_sender
        if http_sender is not None and self._http_transport_is_trusted(http_sender):
            return cast(dict[str, Any] | None, await http_sender.request_action(action))
        return None

    async def _send_action(
        self,
        action: dict[str, Any],
        wait_ws_seconds: float = 0.0,
    ) -> bool | None:
        actions = self._prepare_action_delivery(action)
        receipt = receipt_from_action(action)
        sink = current_action_sink.get()
        captured_by_sink = bool(
            not action.get(ACTION_BYPASS_SINK_KEY, False)
            and sink is not None
            and getattr(sink, "is_active", True)
        )
        sent_all: bool | None = True
        for i, act in enumerate(actions):
            try:
                if i > 0:
                    await asyncio.sleep(MESSAGE_SPLIT_DELAY)
                sent = await self._send_single_action(
                    act,
                    wait_ws_seconds=wait_ws_seconds,
                )
            except BaseException:
                if receipt is not None and not captured_by_sink:
                    await asyncio.shield(receipt.record(False))
                raise
            if receipt is not None and not captured_by_sink:
                # The transport result is the delivery boundary.  Observer
                # cancellation/failure after this point must not roll back a
                # reply that OneBot already accepted.
                await asyncio.shield(receipt.record(sent))
            if sent is True and not captured_by_sink:
                await self._notify_outgoing_action_observers(
                    {key: value for key, value in act.items() if key != DELIVERY_RECEIPT_KEY}
                )
            if sent is False:
                sent_all = False
            elif sent is None and sent_all is True:
                sent_all = None
        return sent_all

    def _prepare_action_delivery(self, action: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize one action at the common delivery boundary.

        Active transports and inbound HTTP/WS responses must expand actions in
        exactly the same way.  Delivery receipts count the expanded actions so
        a multi-part reply cannot commit after only its first chunk.
        """

        actions = self._maybe_split_action(action)
        receipt = receipt_from_action(action)
        if receipt is not None and len(actions) > 1:
            receipt.add_expected_actions(len(actions) - 1)
        return actions

    @staticmethod
    def _tag_action_source(action: dict[str, Any], plugin_name: str) -> dict[str, Any]:
        if not isinstance(action, dict):
            return action
        tagged = dict(action)
        tagged.setdefault("_source_plugin", str(plugin_name or "").strip())
        return tagged

    async def _notify_outgoing_action_observers(self, action: dict[str, Any]) -> None:
        source_plugin = str(action.get("_source_plugin", "") or "").strip()
        if not source_plugin:
            return
        if str(action.get("action", "") or "").strip() not in (
            "send_group_msg",
            "send_private_msg",
        ):
            return

        try:
            loaded, observer_service = self.plugin_manager.resolve_service(
                caller_plugin="core",
                service_name="core.observe_outgoing_action",
            )
        except RuntimeError as exc:
            logger.debug("Outgoing action observer unavailable: %s", exc)
            return

        raw_params = action.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        group_id = params.get("group_id")
        user_id = params.get("user_id")
        try:
            context = self.plugin_manager.build_context(
                observer_service.owner,
                user_id=user_id if group_id in (None, "") else None,
                group_id=group_id,
                # 路由范围由上下文参数提供；生命周期主体不能伪装成用户主体携带群范围。
                principal=self.identity_service.issue(
                    kind="lifecycle",
                ),
            )

            async def run_observer() -> None:
                await call_plugin_callback(
                    observer_service.callback,
                    action,
                    context,
                    source_plugin=source_plugin,
                )

            await invoke_loaded_plugin(loaded, run_observer)
        except (PluginExecutionClosed, PluginExecutionTimeout, PluginExecutionUnavailable):
            logger.debug("Outgoing action observer skipped during plugin unload")
        except Exception as exc:
            logger.debug("Outgoing action observer failed: %s", exc, exc_info=True)

    def _maybe_split_action(self, action: dict[str, Any]) -> list[dict[str, Any]]:
        """将包含过长文本的 action 拆分为多个 action"""
        act_name = action.get("action", "")
        if act_name not in ("send_group_msg", "send_private_msg"):
            return [action]

        params = action.get("params")
        if not isinstance(params, dict):
            return [action]

        message = params.get("message")
        if not isinstance(message, list):
            return [action]

        chunks = split_message_segments(message)
        if len(chunks) <= 1:
            return [action]

        # 保留 action 上的额外字段（如 ACTION_BYPASS_SINK_KEY）
        results = []
        for chunk in chunks:
            new_action = {
                "action": act_name,
                "params": {**params, "message": chunk},
            }
            # 复制非标准字段
            for key in action:
                if key not in ("action", "params"):
                    new_action[key] = action[key]
            results.append(new_action)

        logger.debug(
            "Split long message into %d chunks (action=%s)",
            len(results),
            act_name,
        )
        return results

    async def _send_single_action(
        self, action: dict[str, Any], wait_ws_seconds: float = 0.0
    ) -> bool | None:
        try:
            act = str(action.get("action", "") or "")
            if act in ("send_group_msg", "send_private_msg"):
                params = action.get("params") or {}
                if isinstance(params, dict):
                    logger.info(
                        "Sending: action=%s group=%s user=%s message_length=%s",
                        act,
                        params.get("group_id") or "-",
                        params.get("user_id") or "-",
                        _message_length(params.get("message")),
                    )
        except (KeyError, TypeError, ValueError) as exc:
            # 日志记录失败不影响消息发送，仅记录调试信息
            logger.debug("Failed to generate message preview: %s", exc)
        bypass_sink = bool(action.get(ACTION_BYPASS_SINK_KEY, False))
        sink = current_action_sink.get()
        if not bypass_sink and sink is not None and getattr(sink, "is_active", True):
            await sink(
                {key: value for key, value in action.items() if key != ACTION_BYPASS_SINK_KEY}
            )
            return True

        delivery_action = {
            key: value
            for key, value in action.items()
            if key not in {ACTION_BYPASS_SINK_KEY, DELIVERY_RECEIPT_KEY}
        }

        def copy_delivery_result() -> None:
            if ACTION_RESULT_MESSAGE_ID_KEY in delivery_action:
                action[ACTION_RESULT_MESSAGE_ID_KEY] = delivery_action[ACTION_RESULT_MESSAGE_ID_KEY]

        ws_client = self.ws_client
        if (
            ws_client is not None
            and self._ws_transport_is_trusted(ws_client)
            and ws_client.connected()
        ):
            try:
                sent = await ws_client.send_action(delivery_action)
            except OneBotActionOutcomeUnknown:
                logger.warning(
                    "OneBot WS action was committed but its outcome is unknown; "
                    "delivery fallback is suppressed"
                )
                return None
            if sent:
                copy_delivery_result()
                return True

        # 尝试通过 Inbound WebSocket 广播（如果存在活跃连接）
        if self.inbound_manager and self.inbound_manager.has_active_ws_clients():
            try:
                broadcast_result = await self.inbound_manager.broadcast(delivery_action)
            except Exception as exc:
                logger.warning("Inbound WebSocket broadcast failed; trying fallback: %s", exc)
            else:
                if isinstance(broadcast_result, BroadcastResult) and broadcast_result.delivered:
                    return True
                if not isinstance(broadcast_result, BroadcastResult):
                    logger.error(
                        "Inbound manager returned an invalid broadcast result: %r",
                        type(broadcast_result).__name__,
                    )
                else:
                    logger.warning(
                        "Inbound WebSocket delivered to no clients "
                        "(targets=%d failures=%d timeouts=%d); trying fallback",
                        broadcast_result.target_count,
                        broadcast_result.failure_count,
                        broadcast_result.timeout_count,
                    )
                    if broadcast_result.timeout_count:
                        logger.warning(
                            "Inbound WebSocket delivery outcome is unknown; fallback is suppressed"
                        )
                        return None

        if wait_ws_seconds > 0:
            deadline = asyncio.get_running_loop().time() + float(wait_ws_seconds)
            while asyncio.get_running_loop().time() < deadline:
                candidate = self.ws_client
                if (
                    candidate is not None
                    and self._ws_transport_is_trusted(candidate)
                    and candidate.connected()
                ):
                    try:
                        sent = await candidate.send_action(delivery_action)
                    except OneBotActionOutcomeUnknown:
                        logger.warning(
                            "OneBot WS action was committed but its outcome is unknown; "
                            "retry and HTTP fallback are suppressed"
                        )
                        return None
                    if sent:
                        copy_delivery_result()
                        return True
                await asyncio.sleep(0.1)

        http_sender = self.http_sender
        if http_sender is not None and self._http_transport_is_trusted(http_sender):
            http_sent: bool | None = cast(
                bool | None,
                await http_sender.send_action(delivery_action),
            )
            copy_delivery_result()
            if http_sent is None:
                logger.warning("OneBot HTTP action outcome is unknown")
                return None
            if http_sent is False:
                logger.warning("Action was rejected or not acknowledged by OneBot HTTP")
            return http_sent

        logger.debug("Action dropped: no available sender (ws/http)")
        return False

    async def _collect_actions_for_event(
        self,
        event: dict[str, Any],
        *,
        default_source: str,
    ) -> list[dict[str, Any]]:
        if not await self._claim_inbound_event(event):
            return []
        sink = current_action_sink.get()
        event = dict(event)
        event.setdefault("_source", default_source)

        if sink is not None:
            action = await self._process_event(event)
            return self._prepare_action_delivery(action) if action else []

        collected: list[dict[str, Any]] = []

        async def _collect(action: dict[str, Any]) -> None:
            collected.append(action)

        # 标记 sink 为活动状态
        _collect.is_active = True

        token = current_action_sink.set(_collect)
        try:
            action = await self._process_event(event)
            if action:
                collected.append(action)
        finally:
            # 标记 sink 为失效，使后续（后台任务）调用能直通发送逻辑
            _collect.is_active = False
            current_action_sink.reset(token)

        return [
            prepared for action in collected for prepared in self._prepare_action_delivery(action)
        ]

    async def _claim_inbound_event(self, event: dict[str, Any]) -> bool:
        """Claim a OneBot message id once across inbound HTTP and WS channels."""
        message_id = event.get("message_id")
        if message_id is None or isinstance(message_id, bool):
            return True
        key = self._inbound_event_dedupe_key(event)
        now = time.monotonic()
        async with self._event_dedupe_lock.get():
            self._prune_expired_event_ids(now)
            if key in self._recent_event_ids:
                logger.info("Dropped duplicate inbound OneBot event %r", key)
                return False
            capacity = max(1, MAX_INBOUND_EVENT_DEDUP_KEYS)
            while len(self._recent_event_ids) >= capacity:
                expires_at, _sequence, oldest_key = heapq.heappop(self._event_dedupe_expirations)
                if self._recent_event_ids.get(oldest_key) == expires_at:
                    self._recent_event_ids.pop(oldest_key, None)
                    break
            expires_at = now + INBOUND_EVENT_DEDUP_TTL_SECONDS
            self._event_dedupe_sequence += 1
            self._recent_event_ids[key] = expires_at
            heapq.heappush(
                self._event_dedupe_expirations,
                (expires_at, self._event_dedupe_sequence, key),
            )
            return True

    @staticmethod
    def _inbound_event_dedupe_key(
        event: Mapping[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        """Build a collision-resistant delivery key including conversation scope."""

        return tuple(
            (type(value).__name__, repr(value))
            for value in (
                event.get("self_id"),
                event.get("post_type"),
                event.get("message_type"),
                event.get("group_id"),
                event.get("user_id"),
                event.get("message_id"),
            )
        )

    def _prune_expired_event_ids(self, now: float) -> None:
        """Discard expired keys in heap order without scanning the live map."""

        while self._event_dedupe_expirations and self._event_dedupe_expirations[0][0] <= now:
            expires_at, _sequence, key = heapq.heappop(self._event_dedupe_expirations)
            if self._recent_event_ids.get(key) == expires_at:
                self._recent_event_ids.pop(key, None)

    async def _handle_upstream_event(
        self,
        event: dict[str, Any],
        *,
        source_client: OneBotWsClient | None = None,
    ) -> None:
        """处理来自 OneBot 上游的事件，并拒绝已撤权连接的迟到消息。"""
        if not self._onebot_credentials_are_trusted():
            logger.warning("Dropped upstream OneBot event while credentials are revoked")
            return
        if source_client is not None and not self._ws_transport_is_trusted(source_client):
            logger.warning("Dropped upstream OneBot event from a stale or quarantined client")
            return
        actions = await self._collect_actions_for_event(event, default_source="upstream_ws")
        if not actions:
            return
        for action in actions:
            await self._send_action(action)

    async def _handle_inbound_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """处理来自 Inbound Server 的事件"""
        return await self._collect_actions_for_event(event, default_source="inbound_http")
