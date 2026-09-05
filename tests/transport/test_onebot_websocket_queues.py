"""OneBot WebSocket 事件队列、drainer 与调度并发。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    Any,
    AsyncMock,
    OneBotWsClient,
    _QueuedOneBotEvent,
    asyncio,
    patch,
    pytest,
)

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


class TestOneBotWebSocketQueues:
    """按单一传输职责组织的 OneBot WebSocket 测试。"""

    def test_get_queue_key(self):
        """测试获取队列键"""
        client = OneBotWsClient("ws://localhost:3000", "")

        # 私聊事件
        private_event = {"user_id": 12345, "group_id": None}
        key           = client._get_queue_key(private_event)
        assert key == "user:12345"

        # 群聊事件
        group_event = {"user_id": 12345, "group_id": 67890}
        key         = client._get_queue_key(group_event)
        assert key == "group:67890:user:12345"

        # 无 user_id
        no_user_event = {"group_id": 67890}
        key           = client._get_queue_key(no_user_event)
        assert key is None

    @pytest.mark.asyncio
    async def test_dispatch_event_respects_pending_semaphore_across_queues(self):
        """测试 max_pending_events 真正限制 handler 执行并发"""
        client = OneBotWsClient("ws://localhost:3000", "", max_pending_events=1)
        started  = asyncio.Event()
        release  = asyncio.Event()
        current  = 0
        max_seen = 0

        async def handler(event: dict[str, Any]) -> None:
            nonlocal current, max_seen
            current += 1
            max_seen = max(max_seen, current)
            started.set()
            await release.wait()
            current -= 1

        await asyncio.gather(
            client._dispatch_event(handler, {"user_id": 1}),
            client._dispatch_event(handler, {"user_id": 2}),
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*client._queue_tasks.values())

        assert max_seen == 1

    @pytest.mark.asyncio
    async def test_drain_queue_restarts_when_event_arrives_during_timeout_exit(self):
        """测试 drain 超时退出窗口内入队的事件不会滞留"""
        client                                   = OneBotWsClient("ws://localhost:3000", "")
        key                                      = "user:1"
        queue: asyncio.Queue[_QueuedOneBotEvent] = asyncio.Queue()
        client._message_queues[key]              = queue
        handled: list[dict[str, Any]]            = []
        real_wait_for                            = asyncio.wait_for
        wait_calls                               = 0

        async def handler(event: dict[str, Any]) -> None:
            handled.append(event)

        async def fake_wait_for(awaitable, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                if hasattr(awaitable, "close"):
                    awaitable.close()
                queue.put_nowait(
                    _QueuedOneBotEvent(
                        event      = {"user_id": 1},
                        auth_state = client._endpoint_auth,
                    )
                )
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout)

        task                     = asyncio.create_task(client._drain_queue(key, handler))
        client._queue_tasks[key] = task

        with patch("core.onebot.asyncio.wait_for", side_effect=fake_wait_for):
            await task
            restarted = client._queue_tasks[key]
            await real_wait_for(restarted, timeout=2.0)

        assert handled == [{"user_id": 1}]

    @pytest.mark.asyncio
    async def test_drain_queue_does_not_suppress_an_unexpected_handler_failure(self, monkeypatch):
        client                                   = OneBotWsClient("ws://localhost:3000", "")
        key                                      = "user:unhandled-error"
        queue: asyncio.Queue[_QueuedOneBotEvent] = asyncio.Queue()
        queue.put_nowait(
            _QueuedOneBotEvent(
                event      = {"user_id": 1},
                auth_state = client._endpoint_auth,
            )
        )
        client._message_queues[key] = queue

        async def fail_handler(*_args, **_kwargs):
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(client, "_handle_event_safely", fail_handler)

        with pytest.raises(RuntimeError, match="handler exploded"):
            await client._drain_queue(key, AsyncMock())

    @pytest.mark.asyncio
    async def test_drain_queue_drops_raw_event_without_auth_generation(self):
        client                    = OneBotWsClient("ws://localhost:3000", "")
        key                       = "user:raw-event"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        queue.put_nowait({"user_id": 1})
        client._message_queues[key] = queue
        handler                     = AsyncMock()

        task = asyncio.create_task(client._drain_queue(key, handler))
        for _ in range(10):
            if queue.empty():
                break
            await asyncio.sleep(0)
        assert queue.empty()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        handler.assert_not_awaited()
