"""测试chat插件 - AI对话助手"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugins.chat import main as chat
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = Path(__file__).resolve().parent.parent.parent


class _ChunkedContent:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_chunked(self, _size: int):
        yield self.body


class _BoundedJsonResponse:
    status = 200

    def __init__(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}
        self.content = _ChunkedContent(body)
        self.content_length = len(body)
        self.url = chat.COZE_API_URL


class _BoundedJsonContextManager:
    def __init__(self, payload):
        self.response = _BoundedJsonResponse(payload)

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _BoundedJsonSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _BoundedJsonContextManager(self.payloads.pop(0))


def _completed_chat_payload():
    return {
        "data": {
            "id": "chat-123",
            "conversation_id": "conversation-456",
            "status": "completed",
        }
    }


def _answer_messages_payload(answer: str = "测试回答"):
    return {"data": [{"type": "answer", "content": answer}]}


class TestChatPlugin:
    """测试chat插件"""

    @pytest.fixture
    def mock_context(self, tmp_path):
        """模拟插件上下文"""
        context = MagicMock()
        context.secrets = {
            "plugins": {
                "chat": {
                    "token": "test_token_123",
                    "bot_id": "test_bot_456",
                }
            }
        }
        context.logger = MagicMock()
        context.plugin_dir = ROOT / "plugins" / "chat"
        context.data_dir = tmp_path / "data"
        context.config = {"timezone": "Asia/Shanghai", "plugins": {"chat": {}}}
        context.state = {}
        context.http_session = _BoundedJsonSession(
            _completed_chat_payload(),
            _answer_messages_payload(),
        )

        return with_settings_reader(context)

    @pytest.fixture
    def mock_event(self):
        """模拟事件"""
        return {"user_id": "12345", "message": "test", "message_type": "private"}

    def test_config_helpers_fail_closed_on_wrong_shapes(self):
        assert chat._config_string(1) is None
        assert chat._config_string("") is None
        assert chat._config_string("", allow_empty=True) == ""
        assert chat._config_string(" token ") is None
        assert chat._config_string("x" * (chat.MAX_CONFIG_STRING_LENGTH + 1)) is None

    @pytest.mark.parametrize("value", [True, 0, -1, 1.5, chat.MAX_DAILY_QUOTA + 1])
    def test_quota_limit_rejects_implicit_or_out_of_range_values(self, value):
        with pytest.raises(ValueError, match="daily_user_limit"):
            chat._quota_limit({"daily_user_limit": value}, "daily_user_limit", 20)

    def test_actor_identity_rejects_unbounded_or_control_values(self):
        assert chat._actor_identity(123) == ("123", 123)
        assert chat._actor_identity("admin") == ("admin", "admin")
        assert chat._actor_identity(True) == (chat._ANONYMOUS_ACTOR, None)
        assert chat._actor_identity(-1) == (chat._ANONYMOUS_ACTOR, None)
        assert chat._actor_identity("bad\nactor") == (chat._ANONYMOUS_ACTOR, None)
        assert chat._actor_identity("x" * 129) == (chat._ANONYMOUS_ACTOR, None)

    def test_business_date_falls_back_for_unknown_timezone(self):
        result = chat._business_date({"timezone": "Invalid/Timezone"})

        assert len(result) == 10
        assert result.count("-") == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"data": []},
            {"data": {"id": "chat", "conversation_id": "conversation"}},
            {"data": {"id": "chat", "conversation_id": "conversation", "status": ""}},
            {
                "data": {
                    "id": "chat",
                    "conversation_id": "conversation",
                    "status": "failed\nFORGED_LOG",
                }
            },
        ],
    )
    def test_chat_state_rejects_incomplete_shapes(self, payload):
        assert chat._chat_state(payload) is None

    @pytest.mark.asyncio
    async def test_quota_is_durable_across_context_instances(self, monkeypatch, tmp_path):
        monkeypatch.setattr(chat, "_business_date", lambda _context: "2026-07-14")
        context = SimpleNamespace(data_dir=tmp_path)
        reservation = await chat._reserve_quota(
            context,
            actor="user",
            per_user_limit=1,
            global_limit=1,
            settings_config={},
        )
        reservation.commit()
        with pytest.raises(chat.ChatQuotaExceeded):
            await chat._reserve_quota(
                SimpleNamespace(data_dir=tmp_path),
                actor="user",
                per_user_limit=1,
                global_limit=1,
                settings_config={},
            )

    @pytest.mark.asyncio
    async def test_quota_rejects_corrupt_state_instead_of_resetting_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(chat, "_business_date", lambda _context: "2026-07-14")
        chat.AtomicJsonStore(tmp_path / "chat_quota.json").write(
            {"window": "2026-07-14", "users": [], "total": -1}
        )
        with pytest.raises(chat.ChatQuotaStateError):
            await chat._reserve_quota(
                SimpleNamespace(data_dir=tmp_path),
                actor="user",
                per_user_limit=10,
                global_limit=10,
                settings_config={},
            )

    @pytest.mark.asyncio
    async def test_quota_rollback_ignores_replaced_window(self, tmp_path):
        path = tmp_path / "chat_quota.json"
        chat.AtomicJsonStore(path).write(
            {"window": "2026-07-15", "users": {"user": 1}, "total": 1}
        )
        reservation = chat._QuotaReservation(path, "2026-07-14", "user")

        await reservation.rollback()

        assert reservation.active is False
        assert chat.AtomicJsonStore(path).read(None)["total"] == 1

    @pytest.mark.asyncio
    async def test_service_reply_treats_help_prefix_as_query(self, mock_context, mock_event):
        result = await chat.reply("help me", mock_event, mock_context)

        assert "测试回答" in str(result)
        assert mock_context.http_session.calls

    def test_manifest_defaults_paid_chat_to_admin_only(self):
        import json

        manifest = json.loads(
            (ROOT / "plugins" / "chat" / "plugin.json").read_text(encoding="utf-8")
        )
        assert manifest["commands"][0]["admin_only"] is True

    def test_get_config_valid(self, mock_context):
        """测试获取有效配置"""
        config = chat.get_config(mock_context)
        assert config is not None
        assert config.get("token") == "test_token_123"
        assert config.get("bot_id") == "test_bot_456"

    @pytest.mark.asyncio
    async def test_frozen_snapshot_config_supports_complete_call_chain(self, mock_context):
        from core.config import ConfigSnapshot

        snapshot = ConfigSnapshot(
            config={},
            secrets={
                "plugins": {
                    "chat": {
                        "token": "frozen-token",
                        "bot_id": "frozen-bot",
                    }
                }
            },
        )
        mock_context.secrets = snapshot.secrets

        config = chat.get_config(mock_context)
        assert not isinstance(config, dict)
        assert chat.validate_config(config) == (True, None)
        assert await chat.call_coze_api("冻结配置", config, mock_context) == {
            "messages": [{"type": "answer", "content": "测试回答"}]
        }

    def test_get_config_empty(self):
        """测试空配置"""
        context = MagicMock()
        context.secrets = {}
        context.logger = MagicMock()
        with_settings_reader(context)

        config = chat.get_config(context)
        assert config == {}

    def test_validate_config_valid(self):
        """测试有效配置验证"""
        config = {"token": "test_token", "bot_id": "test_bot"}
        is_valid, error = chat.validate_config(config)
        assert is_valid is True
        assert error is None

    def test_validate_config_empty(self):
        """测试空配置验证"""
        config = {}
        is_valid, error = chat.validate_config(config)
        assert is_valid is False
        assert "配置为空" in error

    def test_validate_config_no_token(self):
        """测试缺少token"""
        config = {"bot_id": "test_bot"}
        is_valid, error = chat.validate_config(config)
        assert is_valid is False
        assert "token" in error

    def test_validate_config_no_bot_id(self):
        """测试缺少bot_id"""
        config = {"token": "test_token"}
        is_valid, error = chat.validate_config(config)
        assert is_valid is False
        assert "bot_id" in error

    @pytest.mark.parametrize(
        ("config", "field"),
        [
            ({"token": 123, "bot_id": "bot"}, "token"),
            ({"token": "token\nleak", "bot_id": "bot"}, "token"),
            ({"token": "token", "bot_id": ["bot"]}, "bot_id"),
            ({"token": "token", "bot_id": "bot", "proxy": True}, "proxy"),
        ],
    )
    def test_validate_config_rejects_non_string_and_control_values(self, config, field):
        valid, error = chat.validate_config(config)

        assert valid is False
        assert field in error

    def test_extract_answer_valid(self, mock_context):
        """测试提取有效答案"""
        data = {"messages": [{"type": "answer", "content": "这是答案"}]}
        answer = chat.extract_answer(data, mock_context)
        assert answer == "这是答案"

    def test_extract_answer_multiple_messages(self, mock_context):
        """测试从多条消息中提取答案"""
        data = {
            "messages": [
                {"type": "question", "content": "问题"},
                {"type": "answer", "content": "答案"},
                {"type": "other", "content": "其他"},
            ]
        }
        answer = chat.extract_answer(data, mock_context)
        assert answer == "答案"

    def test_extract_answer_no_answer(self, mock_context):
        """测试没有答案的情况"""
        data = {"messages": [{"type": "question", "content": "问题"}]}
        answer = chat.extract_answer(data, mock_context)
        assert answer is None

    def test_extract_answer_invalid_type(self, mock_context):
        """测试无效响应类型"""
        answer = chat.extract_answer("not a dict", mock_context)
        assert answer is None

    def test_extract_answer_invalid_messages(self, mock_context):
        """测试无效messages字段"""
        data = {"messages": "not a list"}
        answer = chat.extract_answer(data, mock_context)
        assert answer is None

    def test_extract_answer_empty_content(self, mock_context):
        """测试空答案内容"""
        data = {"messages": [{"type": "answer", "content": "   "}]}
        answer = chat.extract_answer(data, mock_context)
        assert answer is None

    def test_extract_answer_skips_malformed_answer_and_uses_next_text(self, mock_context):
        data = {
            "messages": [
                {"type": "answer", "content": {"unexpected": "object"}},
                {"type": "answer", "content": "  可用答案  "},
            ]
        }

        assert chat.extract_answer(data, mock_context) == "可用答案"

    @pytest.mark.asyncio
    async def test_call_coze_api_success(self, mock_context):
        """测试成功的API调用"""
        query = "测试问题"
        config = {"token": "test_token", "bot_id": "test_bot"}

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is not None
        assert "messages" in result

    @pytest.mark.asyncio
    async def test_call_coze_api_with_proxy(self, mock_context):
        """测试带代理的API调用"""
        query = "测试问题"
        config = {"token": "test_token", "bot_id": "test_bot", "proxy": "http://proxy.example.com"}

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is not None

        assert all(
            kwargs["proxy"] == "http://proxy.example.com"
            for _, _, kwargs in mock_context.http_session.calls
        )

    @pytest.mark.asyncio
    async def test_call_coze_api_uses_v3_non_stream_payload(self, mock_context):
        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
            actor_id="admin",
        )

        assert result == {"messages": [{"type": "answer", "content": "测试回答"}]}
        method, url, kwargs = mock_context.http_session.calls[0]
        assert (method, url) == ("POST", chat.COZE_API_URL)
        assert kwargs["json"]["stream"] is False
        assert kwargs["json"]["auto_save_history"] is True
        assert len(kwargs["json"]["user_id"]) == 32
        assert kwargs["json"]["additional_messages"] == [
            {"role": "user", "content": "测试问题", "content_type": "text"}
        ]
        assert [call[1] for call in mock_context.http_session.calls] == [
            chat.COZE_API_URL,
            chat.COZE_MESSAGES_URL,
        ]

    @pytest.mark.asyncio
    async def test_call_coze_api_polls_until_completed(self, mock_context, monkeypatch):
        mock_context.http_session = _BoundedJsonSession(
            {
                "data": {
                    "id": "chat-123",
                    "conversation_id": "conversation-456",
                    "status": "in_progress",
                }
            },
            _completed_chat_payload(),
            _answer_messages_payload("轮询完成"),
        )
        monkeypatch.setattr(chat, "POLL_INTERVAL_SECONDS", 0)

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result == {"messages": [{"type": "answer", "content": "轮询完成"}]}
        assert [call[1] for call in mock_context.http_session.calls] == [
            chat.COZE_API_URL,
            chat.COZE_RETRIEVE_URL,
            chat.COZE_MESSAGES_URL,
        ]
        assert [call[0] for call in mock_context.http_session.calls] == ["POST", "GET", "GET"]

    @pytest.mark.asyncio
    async def test_call_coze_api_passes_remaining_deadline_to_every_request(
        self,
        mock_context,
        monkeypatch,
    ):
        monkeypatch.setattr(chat, "REQUEST_TIMEOUT", 0.2)
        seen: list[tuple[str, float]] = []

        async def request(_context, _method, url, *, headers, request_kwargs):
            del headers
            seen.append((url, request_kwargs["timeout"]))
            if url == chat.COZE_API_URL:
                await asyncio.sleep(0.03)
                return _completed_chat_payload()
            if url == chat.COZE_MESSAGES_URL:
                return _answer_messages_payload("deadline success")
            raise AssertionError(f"unexpected URL: {url}")

        monkeypatch.setattr(chat, "_request_coze_json", request)

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result == {"messages": [{"type": "answer", "content": "deadline success"}]}
        assert [url for url, _timeout in seen] == [
            chat.COZE_API_URL,
            chat.COZE_MESSAGES_URL,
        ]
        assert 0 < seen[1][1] < seen[0][1] <= chat.REQUEST_TIMEOUT

    @pytest.mark.asyncio
    async def test_call_coze_api_rejects_business_error_without_logging_message(self, mock_context):
        mock_context.http_session = _BoundedJsonSession(
            {"code": 4000, "msg": "REMOTE_CANARY_SHOULD_NOT_BE_LOGGED"}
        )

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result is None
        serialized_logs = repr(mock_context.logger.method_calls)
        assert "REMOTE_CANARY_SHOULD_NOT_BE_LOGGED" not in serialized_logs
        assert "4000" in serialized_logs

    @pytest.mark.asyncio
    async def test_call_coze_api_rejects_non_object_json(self, mock_context):
        mock_context.http_session = _BoundedJsonSession([])

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_call_coze_api_rejects_invalid_create_state(self, mock_context):
        mock_context.http_session = _BoundedJsonSession(
            {"data": {"id": "聊天", "conversation_id": "conversation", "status": "completed"}}
        )

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result is None
        assert len(mock_context.http_session.calls) == 1

    @pytest.mark.asyncio
    async def test_call_coze_api_rejects_terminal_failure_without_fetching_messages(
        self, mock_context
    ):
        mock_context.http_session = _BoundedJsonSession(
            {
                "data": {
                    "id": "chat-123",
                    "conversation_id": "conversation-456",
                    "status": "failed",
                }
            }
        )

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result is None
        assert [call[1] for call in mock_context.http_session.calls] == [chat.COZE_API_URL]

    @pytest.mark.asyncio
    async def test_call_coze_api_rejects_missing_message_list(self, mock_context):
        mock_context.http_session = _BoundedJsonSession(
            _completed_chat_payload(),
            {"data": {"unexpected": "object"}},
        )

        result = await chat.call_coze_api(
            "测试问题",
            {"token": "test_token", "bot_id": "test_bot"},
            mock_context,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_call_coze_api_timeout_attempts_remote_cancel(self, mock_context, monkeypatch):
        monkeypatch.setattr(chat, "REQUEST_TIMEOUT", 0.08)
        monkeypatch.setattr(chat, "POLL_INTERVAL_SECONDS", 0)
        calls: list[tuple[str, float]] = []
        retrieve_cancelled = asyncio.Event()

        async def request(_context, _method, url, *, headers, request_kwargs):
            del headers
            calls.append((url, request_kwargs["timeout"]))
            if url == chat.COZE_API_URL:
                await asyncio.sleep(0.02)
                return {
                    "data": {
                        "id": "chat-123",
                        "conversation_id": "conversation-456",
                        "status": "in_progress",
                    }
                }
            if url == chat.COZE_RETRIEVE_URL:
                try:
                    await asyncio.sleep(5)
                    return _completed_chat_payload()
                except asyncio.CancelledError:
                    retrieve_cancelled.set()
                    raise
            if url == chat.COZE_CANCEL_URL:
                return {"data": {}}
            if url == chat.COZE_MESSAGES_URL:
                return _answer_messages_payload("too late")
            raise AssertionError(f"unexpected URL: {url}")

        monkeypatch.setattr(chat, "_request_coze_json", request)
        result = await asyncio.wait_for(
            chat.call_coze_api(
                "测试问题",
                {"token": "test_token", "bot_id": "test_bot"},
                mock_context,
            ),
            timeout=1.5,
        )

        assert result is None
        assert retrieve_cancelled.is_set()
        assert [url for url, _timeout in calls] == [
            chat.COZE_API_URL,
            chat.COZE_RETRIEVE_URL,
            chat.COZE_CANCEL_URL,
        ]
        assert 0 < calls[1][1] < calls[0][1] <= chat.REQUEST_TIMEOUT

    @pytest.mark.asyncio
    async def test_call_coze_api_rejects_invalid_config_before_http(self, mock_context):
        result = await chat.call_coze_api("测试问题", {"token": 1}, mock_context)

        assert result is None
        assert mock_context.http_session.calls == []

    @pytest.mark.asyncio
    async def test_call_coze_api_error_response(self, mock_context):
        """测试API错误响应"""

        class MockErrorResponse:
            status = 401

            async def text(self):
                return "Unauthorized"

        class MockErrorContextManager:
            async def __aenter__(self):
                return MockErrorResponse()

            async def __aexit__(self, *args):
                pass

        class MockErrorSession:
            def request(self, *args, **kwargs):
                return MockErrorContextManager()

        mock_context.http_session = MockErrorSession()

        query = "测试问题"
        config = {"token": "invalid_token", "bot_id": "test_bot"}

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_call_coze_api_timeout(self, mock_context):
        """测试API超时"""

        class MockTimeoutContextManager:
            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, *args):
                pass

        class MockTimeoutSession:
            def request(self, *args, **kwargs):
                return MockTimeoutContextManager()

        mock_context.http_session = MockTimeoutSession()

        query = "测试问题"
        config = {"token": "test_token", "bot_id": "test_bot"}

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_call_coze_api_exception(self, mock_context):
        """测试API异常"""

        class MockExceptionContextManager:
            async def __aenter__(self):
                raise Exception("Network error")

            async def __aexit__(self, *args):
                pass

        class MockExceptionSession:
            def request(self, *args, **kwargs):
                return MockExceptionContextManager()

        mock_context.http_session = MockExceptionSession()

        query = "测试问题"
        config = {"token": "test_token", "bot_id": "test_bot"}

        result = await chat.call_coze_api(query, config, mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_handle_help_command(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await chat.handle("chat", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "AI" in result_text or "对话" in result_text

    @pytest.mark.parametrize("query", ["帮助我解释这个问题"])
    @pytest.mark.asyncio
    async def test_handle_help_prefix_is_a_normal_query(self, mock_context, mock_event, query):
        """没有空格分隔的中文正文不是 help 子命令。"""

        result = await chat.handle("chat", query, mock_event, mock_context)

        assert "测试回答" in str(result)
        assert mock_context.http_session.calls

    @pytest.mark.asyncio
    async def test_handle_help_with_extra_arg_fails_before_remote(self, mock_context, mock_event):
        result = await chat.handle("chat", "help extra", mock_event, mock_context)

        assert "不接受额外参数" in str(result)
        assert "/chat help" in str(result)
        assert mock_context.http_session.calls == []

    @pytest.mark.asyncio
    async def test_handle_empty_query(self, mock_context, mock_event):
        """测试空查询"""
        result = await chat.handle("chat", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "请输入" in result_text or "用法" in result_text

    @pytest.mark.asyncio
    async def test_handle_invalid_config(self, mock_context, mock_event):
        """测试无效配置"""
        mock_context.secrets = {}
        result = await chat.handle("chat", "测试问题", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "配置错误" in result_text or "配置" in result_text

    @pytest.mark.asyncio
    async def test_handle_query_too_long(self, mock_context, mock_event):
        """测试查询过长"""
        long_query = "a" * 2500
        result = await chat.handle("chat", long_query, mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "过长" in result_text or "字符" in result_text

    @pytest.mark.asyncio
    async def test_handle_rejects_invalid_quota_without_calling_remote(
        self, mock_context, mock_event
    ):
        mock_context.config["plugins"]["chat"]["daily_user_limit"] = True

        result = await chat.handle("chat", "测试问题", mock_event, mock_context)

        assert "daily_user_limit" in str(result)
        assert mock_context.http_session.calls == []

    @pytest.mark.asyncio
    async def test_handle_success(self, mock_context, mock_event):
        """测试成功的对话处理"""
        result = await chat.handle("chat", "你好", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_gpt_alias(self, mock_context, mock_event):
        """测试gpt命令别名"""
        result = await chat.handle("gpt", "测试", mock_event, mock_context)
        assert result is not None

    @pytest.mark.asyncio
    async def test_handle_ai_alias(self, mock_context, mock_event):
        """测试ai命令别名"""
        result = await chat.handle("ai", "测试", mock_event, mock_context)
        assert result is not None

    @pytest.mark.asyncio
    async def test_declared_reply_service_uses_same_handler(self, mock_context, mock_event):
        result = await chat.reply("测试", mock_event, mock_context)

        assert "测试回答" in str(result)

    def test_help_text(self):
        """测试帮助文本"""
        help_text = chat.HELP_TEXT
        assert help_text is not None
        assert "AI" in help_text or "对话" in help_text
        assert "/chat" in help_text

    def test_constants(self):
        """测试常量定义"""
        assert hasattr(chat, "COZE_API_URL")
        assert hasattr(chat, "REQUEST_TIMEOUT")
        assert hasattr(chat, "MAX_QUERY_LENGTH")

        assert chat.COZE_API_URL == "https://api.coze.com/v3/chat"
        assert chat.REQUEST_TIMEOUT == 30
        assert chat.MAX_QUERY_LENGTH == 2000
