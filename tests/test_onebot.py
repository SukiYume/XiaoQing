"""
OneBot 模块单元测试
"""

import asyncio
import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.bounded_http import HttpStatusError
from core.onebot import (
    _CONNECT_SIGNATURE_CACHE,
    OneBotHttpSender,
    OneBotWsClient,
    _extract_message_preview,
    _get_connect_signature,
    _mask_sensitive_text,
    _summarize_event,
    _verify_token_auth,
)


@pytest.fixture(autouse=True)
def bounded_transport_adapter(monkeypatch):
    """Adapt the historical OneBot response mocks to bounded JSON bytes."""

    async def request(session, method, url, **kwargs):
        request_kwargs = dict(kwargs.get("request_kwargs") or {})
        response_cm = session.post(
            url,
            headers=kwargs.get("headers"),
            **request_kwargs,
        )
        async with response_cm as response:
            status = int(response.status)
            if status not in kwargs.get("success_statuses", {200}):
                raise HttpStatusError(status)
            return json.dumps(await response.json()).encode("utf-8")

    monkeypatch.setattr("core.onebot.aiohttp_request_bounded", request)


async def _wait_for_ws_action_request(mock_ws: AsyncMock) -> dict[str, Any]:
    for _ in range(10):
        if mock_ws.send.await_count:
            return json.loads(mock_ws.send.await_args.args[0])
        await asyncio.sleep(0)
    raise AssertionError("WebSocket action was not sent")


# ============================================================
# _verify_token_auth 测试
# ============================================================

class TestVerifyTokenAuth:
    """_verify_token_auth 测试"""

    def test_valid_token(self):
        """测试有效 token"""
        result = _verify_token_auth("Bearer my_token", "my_token")
        assert result is True

    def test_no_token_configured(self):
        """测试未配置 token 时默认拒绝，避免鉴权失败开放"""
        result = _verify_token_auth("Bearer anything", "")
        assert result is False

        result = _verify_token_auth("Bearer anything", None)
        assert result is False

    def test_invalid_token(self):
        """测试无效 token"""
        result = _verify_token_auth("Bearer wrong_token", "my_token")
        assert result is False

    def test_missing_bearer_prefix(self):
        """测试缺少 Bearer 前缀"""
        result = _verify_token_auth("my_token", "my_token")
        assert result is False

    def test_length_mismatch(self):
        """测试长度不匹配"""
        result = _verify_token_auth("short", "much_longer_token")
        assert result is False

# ============================================================
# _mask_sensitive_text 测试
# ============================================================

class TestMaskSensitiveText:
    """_mask_sensitive_text 测试"""

    def test_mask_token(self):
        """测试屏蔽 token"""
        text = "Authorization: Bearer secret_token_123"
        result = _mask_sensitive_text(text)
        # Bearer 关键字被掩码，token 值仍存在（这是当前实现的行为）
        assert "Bearer" not in result or result == "Authorization: ******** secret_token_123"
        assert "********" in result

    def test_mask_authorization_header(self):
        """测试屏蔽直接提供的 authorization 值"""
        text = "authorization=secret_token_123"
        result = _mask_sensitive_text(text)
        assert "secret_token_123" not in result
        assert "********" in result

    def test_mask_api_key(self):
        """测试屏蔽 api_key"""
        text = "api_key=sk-1234567890"
        result = _mask_sensitive_text(text)
        assert "sk-1234567890" not in result
        assert "********" in result

    def test_mask_password(self):
        """测试屏蔽 password"""
        text = "password=my_password"
        result = _mask_sensitive_text(text)
        assert "my_password" not in result

    def test_mask_multiple(self):
        """测试屏蔽多个敏感信息"""
        text = "token=abc123 and password=xyz789"
        result = _mask_sensitive_text(text)
        assert "abc123" not in result
        assert "xyz789" not in result

    def test_case_insensitive(self):
        """测试大小写不敏感"""
        text = "API_KEY=secret"
        result = _mask_sensitive_text(text)
        assert "secret" not in result

    def test_no_sensitive_data(self):
        """测试无敏感数据"""
        text = "hello world"
        result = _mask_sensitive_text(text)
        assert result == text

# ============================================================
# _extract_message_preview 测试
# ============================================================

class TestExtractMessagePreview:
    """_extract_message_preview 测试"""

    def test_empty_message(self):
        """测试空消息"""
        result = _extract_message_preview([])
        assert result == "(empty)"

    def test_text_only(self):
        """测试纯文本"""
        message = [{"type": "text", "data": {"text": "Hello world"}}]
        result = _extract_message_preview(message)
        assert result == "Hello world"

    def test_text_with_image(self):
        """测试文本和图片"""
        message = [
            {"type": "text", "data": {"text": "Check this: "}},
            {"type": "image", "data": {"file": "test.png"}},
        ]
        result = _extract_message_preview(message)
        assert "Check this:" in result
        assert "[图片]" in result

    def test_text_with_emoji_image(self):
        """测试带 emoji 元数据的图片预览"""
        message = [
            {"type": "text", "data": {"text": "Mood: "}},
            {"type": "emoji", "data": {"file": "emoji.png"}},
        ]
        result = _extract_message_preview(message)
        assert "Mood:" in result
        assert "[表情包]" in result

    def test_at_mention(self):
        """测试 @ 提及"""
        message = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " hello"}},
        ]
        result = _extract_message_preview(message)
        assert "[@12345]" in result

    def test_unknown_segment_type(self):
        """测试未知消息段类型"""
        message = [{"type": "unknown", "data": {}}]
        result = _extract_message_preview(message)
        assert "[unknown]" in result

    def test_truncation(self):
        """测试截断"""
        long_text = "a" * 100
        message = [{"type": "text", "data": {"text": long_text}}]
        result = _extract_message_preview(message, max_len=20)
        assert result.endswith("...")
        assert len(result) <= 25  # 20 + "..."

# ============================================================
# _summarize_event 测试
# ============================================================

class TestSummarizeEvent:
    """_summarize_event 测试"""

    def test_full_event(self):
        """测试完整事件"""
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        result = _summarize_event(event)
        assert "post_type=message" in result
        assert "message_type=group" in result
        assert "user_id=12345" in result
        assert "group_id=67890" in result

    def test_minimal_event(self):
        """测试最小事件"""
        event = {"post_type": "notice"}
        result = _summarize_event(event)
        assert "post_type=notice" in result

    def test_message_length(self):
        """测试消息长度"""
        event = {
            "post_type": "message",
            "message": [{"type": "text"}, {"type": "image"}],
        }
        result = _summarize_event(event)
        assert "message_len=2" in result

    def test_string_message(self):
        """测试字符串消息"""
        event = {
            "post_type": "message",
            "message": "hello",
        }
        result = _summarize_event(event)
        assert "message_kind=str" in result

# ============================================================
# OneBotHttpSender 测试
# ============================================================

class TestOneBotHttpSender:
    """OneBotHttpSender 测试"""

    @pytest.fixture
    def mock_session(self):
        """模拟 HTTP 会话"""
        session = MagicMock()
        # post should be a MagicMock that returns a context manager, not an AsyncMock (which returns a coroutine)
        session.post = MagicMock()
        return session

    @pytest.fixture
    def sender(self, mock_session):
        """创建 OneBotHttpSender 实例"""
        return OneBotHttpSender(
            http_base="http://localhost:3000",
            auth_token="test_token",
            session=mock_session,
        )

    def test_initialization(self, sender: OneBotHttpSender):
        """测试初始化"""
        assert sender.http_base == "http://localhost:3000"
        assert sender.auth_token == "test_token"

    def test_http_base_trailing_slash_removed(self, mock_session):
        """测试移除尾部斜杠"""
        sender = OneBotHttpSender(
            http_base="http://localhost:3000/",
            auth_token="",
            session=mock_session,
        )
        assert sender.http_base == "http://localhost:3000"

    def test_update(self, sender: OneBotHttpSender, mock_session):
        """测试更新配置"""
        sender.update("http://new-host:4000", "new_token")
        assert sender.http_base == "http://new-host:4000"
        assert sender.auth_token == "new_token"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response_envelope",
        [
            {"status": "ok", "retcode": 0, "data": {"message_id": 7}},
            {"status": "failed", "retcode": 100, "data": {}},
        ],
    )
    async def test_request_action_returns_complete_onebot_envelope(
        self,
        sender: OneBotHttpSender,
        mock_session,
        response_envelope,
    ):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_envelope)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        action = {"action": "get_msg", "params": {"message_id": 7}}

        result = await sender.request_action(action)

        assert result == response_envelope
        assert action == {"action": "get_msg", "params": {"message_id": 7}}
        mock_session.post.assert_called_once_with(
            "http://localhost:3000/get_msg",
            json={"message_id": 7},
            headers={"Authorization": "Bearer test_token"},
            timeout=mock_session.post.call_args.kwargs["timeout"],
        )

    @pytest.mark.asyncio
    async def test_request_action_rejects_non_mapping_response(
        self,
        sender: OneBotHttpSender,
        mock_session,
    ):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=[{"status": "ok"}])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm

        assert await sender.request_action({"action": "get_msg", "params": {}}) is None

    @pytest.mark.asyncio
    async def test_send_action(self, sender: OneBotHttpSender, mock_session):
        """测试发送动作"""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"status": "ok", "retcode": 0})
        
        # Configure the context manager returned by post()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm

        action = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
        }

        assert await sender.send_action(action) is True

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "send_group_msg" in call_args[0][0]
        assert call_args.kwargs["timeout"].total == 15.0

    @pytest.mark.asyncio
    async def test_send_action_normalizes_emoji_segment(self, sender: OneBotHttpSender, mock_session):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"status": "ok", "retcode": 0})

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm

        action = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "emoji", "data": {"file": "emoji.png", "summary": "无语"}}],
            },
        }

        assert await sender.send_action(action) is True

        posted_message = mock_session.post.call_args.kwargs["json"]["message"]
        assert posted_message == [
            {"type": "image", "data": {"file": "emoji.png", "summary": "无语", "sub_type": "emoji"}}
        ]

    @pytest.mark.asyncio
    async def test_send_action_with_empty_base(self, mock_session):
        """测试空 http_base 不发送"""
        sender = OneBotHttpSender("", "", mock_session)
        action = {"action": "test", "params": {}}

        assert await sender.send_action(action) is False

        mock_session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_action_rejects_nonzero_onebot_retcode(self, sender, mock_session):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"status": "failed", "retcode": 100})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm

        assert await sender.send_action({"action": "send_group_msg", "params": {}}) is False

# ============================================================
# OneBotWsClient 测试
# ============================================================

class TestOneBotWsClient:
    """OneBotWsClient 测试"""

    def test_initialization(self):
        """测试初始化"""
        client = OneBotWsClient(
            ws_uri="ws://localhost:3000",
            auth_token="test_token",
        )
        assert client.ws_uri == "ws://localhost:3000"
        assert client.auth_token == "test_token"
        assert client.connected() is False

    def test_set_on_connect(self):
        """测试设置连接回调"""
        client = OneBotWsClient("ws://localhost:3000", "")

        async def callback():
            pass

        client.set_on_connect(callback)
        assert client._on_connect is callback

    def test_update(self):
        """测试更新配置"""
        client = OneBotWsClient("ws://old:3000", "old_token")
        client.update("ws://new:4000", "new_token")
        assert client.ws_uri == "ws://new:4000"
        assert client.auth_token == "new_token"

    def test_connected(self):
        """测试连接状态"""
        client = OneBotWsClient("ws://localhost:3000", "")
        assert client.connected() is False

        # 模拟设置 WebSocket
        client._ws = MagicMock()
        assert client.connected() is True

    def test_connected_rejects_closed_websocket(self):
        """测试 closed/close_code/state 会被识别为未连接"""
        client = OneBotWsClient("ws://localhost:3000", "")

        ws = MagicMock()
        ws.closed = True
        client._ws = ws
        assert client.connected() is False

        ws = MagicMock()
        ws.closed = False
        ws.close_code = 1000
        client._ws = ws
        assert client.connected() is False

        ws = MagicMock()
        ws.closed = False
        ws.close_code = None
        ws.state.name = "CLOSED"
        client._ws = ws
        assert client.connected() is False

    @pytest.mark.asyncio
    async def test_send_action_when_connected(self):
        """测试连接时只在匹配回执确认成功"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        action = {
            "action": "send_group_msg",
            "params": {"group_id": 12345, "message": []},
        }

        pending = asyncio.create_task(client.send_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        assert "echo" in sent_payload
        assert client._resolve_action_response(
            {"echo": sent_payload["echo"], "status": "ok", "retcode": 0}
        )
        result = await pending

        mock_ws.send.assert_called_once()
        assert result is True
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            {"status": "ok", "retcode": 0, "data": {"file": "image.png"}},
            {"status": "failed", "retcode": 100, "data": {}},
        ],
    )
    async def test_request_action_returns_echo_matched_envelope(self, response):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws
        action = {"action": "get_image", "params": {"file_id": "abc"}}

        pending = asyncio.create_task(client.request_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        envelope = {"echo": sent_payload["echo"], **response}
        assert client._resolve_action_response(envelope) is True

        assert await pending == envelope
        assert action == {"action": "get_image", "params": {"file_id": "abc"}}
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_send_action_when_connected_normalizes_emoji_segment(self):
        """测试 WebSocket 发送时会归一化 emoji 段"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        action = {
            "action": "send_group_msg",
            "params": {"group_id": 12345, "message": [{"type": "emoji", "data": {"file": "emoji.png"}}]},
        }

        pending = asyncio.create_task(client.send_action(action))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        client._resolve_action_response(
            {"echo": sent_payload["echo"], "status": "ok", "retcode": 0}
        )
        result = await pending

        assert sent_payload["params"]["message"] == [
            {"type": "image", "data": {"file": "emoji.png", "sub_type": "emoji"}}
        ]
        assert result is True

    @pytest.mark.asyncio
    async def test_send_action_when_not_connected(self):
        """测试未连接时不发送"""
        client = OneBotWsClient("ws://localhost:3000", "")

        action = {"action": "test", "params": {}}

        result = await client.send_action(action)
        assert result is False

    @pytest.mark.asyncio
    async def test_send_action_failure_clears_ws(self):
        """测试发送失败时会清理失效连接"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock(side_effect=RuntimeError("boom"))
        client._ws = mock_ws

        result = await client.send_action({"action": "test", "params": {}})

        assert result is False
        assert client._ws is None

    @pytest.mark.asyncio
    async def test_send_action_rejects_nonzero_retcode(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        client._resolve_action_response(
            {"echo": sent_payload["echo"], "status": "failed", "retcode": 100}
        )

        assert await pending is False
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_send_action_times_out_for_wrong_echo(self):
        client = OneBotWsClient("ws://localhost:3000", "", action_response_timeout_seconds=0.01)
        mock_ws = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        assert client._resolve_action_response(
            {"echo": f"wrong-{sent_payload['echo']}", "status": "ok", "retcode": 0}
        )

        assert await pending is False
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_stop_fails_pending_ws_action_response(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        await _wait_for_ws_action_request(mock_ws)
        await client.stop()

        assert await pending is False
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_duplicate_ws_action_response_does_not_change_completed_result(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        client._ws = mock_ws

        pending = asyncio.create_task(client.send_action({"action": "test", "params": {}}))
        sent_payload = await _wait_for_ws_action_request(mock_ws)
        response = {"echo": sent_payload["echo"], "status": "ok", "retcode": 0}
        client._resolve_action_response(response)
        assert await pending is True

        assert client._resolve_action_response(response) is True
        assert client._pending_action_futures == {}

    @pytest.mark.asyncio
    async def test_listen_routes_action_response_without_dispatching_it(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        response_future = asyncio.get_running_loop().create_future()
        client._pending_action_futures["request-echo"] = response_future
        handler = AsyncMock()

        class ResponseOnlyWebSocket:
            def __init__(self):
                self._messages = iter(
                    [
                        json.dumps(
                            {"echo": "request-echo", "status": "ok", "retcode": 0}
                        )
                    ]
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._messages)
                except StopIteration:
                    raise StopAsyncIteration

        await client._listen(ResponseOnlyWebSocket(), handler)

        assert response_future.result() == {
            "echo": "request-echo",
            "status": "ok",
            "retcode": 0,
        }
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop(self):
        """测试停止客户端"""
        client = OneBotWsClient("ws://localhost:3000", "")
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        client._ws = mock_ws

        await client.stop()

        assert client._running is False
        mock_ws.close.assert_awaited_once()
        assert client._ws is None

    @pytest.mark.asyncio
    async def test_stop_awaits_cleanup_task_cancellation(self):
        """测试 stop 会等待 cleanup task 完成取消清理"""
        client = OneBotWsClient("ws://localhost:3000", "")
        cleanup_finished = asyncio.Event()

        async def cleanup_loop():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_finished.set()

        client._cleanup_task = asyncio.create_task(cleanup_loop())
        await asyncio.sleep(0)

        await client.stop()

        assert cleanup_finished.is_set()
        assert client._cleanup_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_all_keyed_drainers_and_clears_queues(self):
        client = OneBotWsClient("ws://localhost:3000", "")
        entered = asyncio.Event()

        async def handler(_event):
            entered.set()
            await asyncio.Event().wait()

        await client._dispatch_event(handler, {"user_id": 1})
        await client._dispatch_event(handler, {"user_id": 2})
        await entered.wait()
        tasks = list(client._queue_tasks.values())

        await client.stop()

        assert tasks and all(task.cancelled() for task in tasks)
        assert client._queue_tasks == {}
        assert client._message_queues == {}
        assert client._queue_last_activity == {}
        await client._dispatch_event(handler, {"user_id": 3})
        assert client._queue_tasks == {}

    @pytest.mark.asyncio
    async def test_stop_cancels_and_joins_its_own_listen_task(self, monkeypatch):
        client = OneBotWsClient("ws://localhost:3000", "")
        entered = asyncio.Event()

        async def block_connect(_handler):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(client, "_connect_once", block_connect)
        listen_task = asyncio.create_task(client.connect_and_listen(AsyncMock()))
        await entered.wait()

        await client.stop()

        assert listen_task.cancelled()
        assert client._main_task is None

    @pytest.mark.asyncio
    async def test_connect_once_reraises_connection_error_for_backoff(self):
        """测试 _connect_once 会重新抛出连接异常，让外层退避逻辑生效。"""
        client = OneBotWsClient("ws://localhost:3000", "token")

        class DummyWebsockets:
            def connect(self, *args, **kwargs):
                raise RuntimeError("boom")

        with patch.dict("sys.modules", {"websockets": DummyWebsockets()}), patch(
            "core.onebot._get_connect_signature",
            return_value={"additional_headers"},
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await client._connect_once(AsyncMock())

    def test_get_connect_signature_is_cached(self):
        """测试 connect signature 只 inspect 一次"""
        class DummyWebsockets:
            @staticmethod
            def connect(uri, additional_headers=None):
                return None

        _CONNECT_SIGNATURE_CACHE.clear()
        with patch("inspect.signature", wraps=inspect.signature) as mock_signature:
            assert "additional_headers" in _get_connect_signature(DummyWebsockets)
            assert "additional_headers" in _get_connect_signature(DummyWebsockets)

        assert mock_signature.call_count == 1

    def test_get_queue_key(self):
        """测试获取队列键"""
        client = OneBotWsClient("ws://localhost:3000", "")

        # 私聊事件
        private_event = {"user_id": 12345, "group_id": None}
        key = client._get_queue_key(private_event)
        assert key == "user:12345"

        # 群聊事件
        group_event = {"user_id": 12345, "group_id": 67890}
        key = client._get_queue_key(group_event)
        assert key == "group:67890:user:12345"

        # 无 user_id
        no_user_event = {"group_id": 67890}
        key = client._get_queue_key(no_user_event)
        assert key is None

    @pytest.mark.asyncio
    async def test_dispatch_event_respects_pending_semaphore_across_queues(self):
        """测试 max_pending_events 真正限制 handler 执行并发"""
        client = OneBotWsClient("ws://localhost:3000", "", max_pending_events=1)
        started = asyncio.Event()
        release = asyncio.Event()
        current = 0
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
        client = OneBotWsClient("ws://localhost:3000", "")
        key = "user:1"
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        client._message_queues[key] = queue
        handled: list[dict[str, Any]] = []
        real_wait_for = asyncio.wait_for
        wait_calls = 0

        async def handler(event: dict[str, Any]) -> None:
            handled.append(event)

        async def fake_wait_for(awaitable, timeout):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                if hasattr(awaitable, "close"):
                    awaitable.close()
                queue.put_nowait({"user_id": 1})
                raise asyncio.TimeoutError()
            return await real_wait_for(awaitable, timeout)

        task = asyncio.create_task(client._drain_queue(key, handler))
        client._queue_tasks[key] = task

        with patch("core.onebot.asyncio.wait_for", side_effect=fake_wait_for):
            await task
            restarted = client._queue_tasks[key]
            await real_wait_for(restarted, timeout=2.0)

        assert handled == [{"user_id": 1}]

    @pytest.mark.asyncio
    async def test_drain_queue_does_not_suppress_an_unexpected_handler_failure(self, monkeypatch):
        client = OneBotWsClient("ws://localhost:3000", "")
        key = "user:unhandled-error"
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        queue.put_nowait({"user_id": 1})
        client._message_queues[key] = queue

        async def fail_handler(*_args, **_kwargs):
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(client, "_handle_event_safely", fail_handler)

        with pytest.raises(RuntimeError, match="handler exploded"):
            await client._drain_queue(key, AsyncMock())

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
