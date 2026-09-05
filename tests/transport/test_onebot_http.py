"""OneBot HTTP 发送器。"""

from __future__ import annotations

import tests.helpers.onebot_test_support as _fixture_support
from tests.helpers.onebot_test_support import (
    AsyncMock,
    MagicMock,
    OneBotHttpSender,
    _normalize_action_for_onebot,
    asyncio,
    json,
    patch,
    pytest,
)

bounded_transport_adapter = _fixture_support.bounded_transport_adapter


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
            http_base  = "http://localhost:3000",
            auth_token = "test_token",
            session    = mock_session,
        )

    def test_initialization(self, sender: OneBotHttpSender):
        """测试初始化"""
        assert sender.http_base == "http://localhost:3000"
        assert sender.auth_token == "test_token"

    def test_http_base_trailing_slash_removed(self, mock_session):
        """测试移除尾部斜杠"""
        sender = OneBotHttpSender(
            http_base  = "http://localhost:3000/",
            auth_token = "",
            session    = mock_session,
        )
        assert sender.http_base == "http://localhost:3000"

    def test_update(self, sender: OneBotHttpSender, mock_session):
        """测试更新配置"""
        sender.update("http://new-host:4000", "new_token")
        assert sender.http_base == "http://new-host:4000"
        assert sender.auth_token == "new_token"

    @pytest.mark.asyncio
    async def test_revoked_credentials_never_start_an_anonymous_http_request(
        self,
        sender: OneBotHttpSender,
        monkeypatch,
    ):
        request = AsyncMock()
        monkeypatch.setattr("core.onebot.aiohttp_request_bounded", request)

        sender.update(sender.http_base, "", credentials_trusted=False)

        assert sender.credentials_trusted is False
        assert await sender.request_action({"action": "get_status", "params": {}}) is None
        request.assert_not_awaited()

        sender.update(sender.http_base, "", credentials_trusted=True)
        assert sender.credentials_trusted is True

    @pytest.mark.asyncio
    async def test_request_uses_one_atomic_endpoint_auth_snapshot(
        self,
        sender: OneBotHttpSender,
        monkeypatch,
    ):
        entered                                    = asyncio.Event()
        release                                    = asyncio.Event()
        captured: list[tuple[str, dict[str, str]]] = []

        async def request(_session, _method, url, **kwargs):
            captured.append((url, dict(kwargs["headers"])))
            entered.set()
            await release.wait()
            return json.dumps({"status": "ok", "retcode": 0, "data": {}}).encode()

        monkeypatch.setattr("core.onebot.aiohttp_request_bounded", request)
        task = asyncio.create_task(sender.request_action({"action": "get_status", "params": {}}))
        await entered.wait()

        sender.update("http://new-host:4000", "new_token")
        release.set()
        await task

        assert captured == [
            (
                "http://localhost:3000/get_status",
                {"Authorization": "Bearer test_token"},
            )
        ]
        assert (sender.http_base, sender.auth_token) == (
            "http://new-host:4000",
            "new_token",
        )

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
        mock_response        = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=response_envelope)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        action                         = {"action": "get_msg", "params": {"message_id": 7}}

        result = await sender.request_action(action)

        assert result == response_envelope
        assert action == {"action": "get_msg", "params": {"message_id": 7}}
        mock_session.post.assert_called_once_with(
            "http://localhost:3000/get_msg",
            json    = {"message_id": 7},
            headers = {"Authorization": "Bearer test_token"},
            timeout = mock_session.post.call_args.kwargs["timeout"],
        )

    @pytest.mark.asyncio
    async def test_request_action_rejects_non_mapping_response(
        self,
        sender: OneBotHttpSender,
        mock_session,
    ):
        mock_response        = AsyncMock()
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
        mock_response        = AsyncMock()
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

        with patch(
            "core.onebot._normalize_action_for_onebot",
            wraps=_normalize_action_for_onebot,
        ) as normalize:
            assert await sender.send_action(action) is True

        assert normalize.call_count == 1

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "send_group_msg" in call_args[0][0]
        assert call_args.kwargs["timeout"].total == 15.0

    @pytest.mark.asyncio
    async def test_send_action_normalizes_emoji_segment(
        self, sender: OneBotHttpSender, mock_session
    ):
        mock_response        = AsyncMock()
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
    async def test_send_action_normalizes_scalar_text_before_http_commit(
        self, sender: OneBotHttpSender, monkeypatch
    ):
        request = AsyncMock(
            return_value=json.dumps({"status": "ok", "retcode": 0, "data": {}}).encode()
        )
        monkeypatch.setattr("core.onebot.aiohttp_request_bounded", request)
        action = {
            "action": "send_group_msg",
            "params": {
                "group_id": 12345,
                "message": [{"type": "text", "data": {"text": 123}}],
            },
        }

        assert await sender.send_action(action) is True
        assert request.await_args.kwargs["request_kwargs"]["json"]["message"] == [
            {"type": "text", "data": {"text": "123"}}
        ]

    @pytest.mark.asyncio
    async def test_send_action_rejects_bad_segment_data_before_http_commit(
        self, sender: OneBotHttpSender, monkeypatch
    ):
        request = AsyncMock()
        monkeypatch.setattr("core.onebot.aiohttp_request_bounded", request)
        action = {
            "action": "send_group_msg",
            "params": {"group_id": 12345, "message": [{"type": "text", "data": "bad"}]},
        }

        with pytest.raises(TypeError, match="segment data"):
            await sender.send_action(action)
        request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_action_with_empty_base(self, mock_session):
        """测试空 http_base 不发送"""
        sender = OneBotHttpSender("", "", mock_session)
        action = {"action": "test", "params": {}}

        assert await sender.send_action(action) is False

        mock_session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_action_rejects_nonzero_onebot_retcode(self, sender, mock_session):
        mock_response        = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"status": "failed", "retcode": 100})
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_response)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm

        assert await sender.send_action({"action": "send_group_msg", "params": {}}) is False

    @pytest.mark.asyncio
    async def test_send_action_preserves_unknown_http_outcome(self, sender, monkeypatch):
        monkeypatch.setattr(
            "core.onebot.aiohttp_request_bounded",
            AsyncMock(side_effect=asyncio.TimeoutError),
        )

        assert await sender.send_action({"action": "send_group_msg", "params": {}}) is None
