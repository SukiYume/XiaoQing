"""Signin 的路由、配置、传输和第三方响应契约测试。"""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.config import ConfigSnapshot
from plugins.signin import main as signin
from plugins.signin import yingshi
from tests.aiohttp_fakes import wrap_legacy_aiohttp_session
from tests.helpers.assertions import text_segments_text
from tests.helpers.settings_snapshot import settings_snapshot

ROOT = Path(__file__).resolve().parents[2]


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._json_data = payload
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self) -> None:
        self.responses: list[_Response] = []
        self.calls: list[dict[str, object]] = []

    def queue(self, *responses: _Response) -> None:
        self.responses.extend(responses)

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected signin HTTP request")
        return self.responses.pop(0)


class _Context:
    def __init__(self, *, configured: bool = True, with_http: bool = True) -> None:
        self.request_id = "signin-test-request"
        self.logger = logging.getLogger("test.signin")
        self.raw_session = _Session()
        self.http_session = wrap_legacy_aiohttp_session(self.raw_session) if with_http else None
        platform_config = (
            {
                "app_id": "test_app_id",
                "kdt_id": "test_kdt_id",
                "access_token": "test_access_token",
                "sid": "test_sid",
            }
            if configured
            else {}
        )
        self.secrets = {"plugins": {"signin": {"yingshijufeng": platform_config}}}

    def queue(self, *payloads: object) -> None:
        self.raw_session.queue(*(_Response(payload) for payload in payloads))

    def get_settings_snapshot(self):
        secrets = self.secrets if isinstance(self.secrets, Mapping) else {}
        return settings_snapshot(secrets=secrets)


class _FalseySession:
    """验证合法但 falsey 的会话不会被误判为未初始化。"""

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def __bool__(self) -> bool:
        return False

    def request(self, *args, **kwargs):
        return self.delegate.request(*args, **kwargs)


@pytest.fixture
def context() -> _Context:
    return _Context()


@pytest.fixture
def event() -> dict[str, object]:
    return {"user_id": "12345", "message_type": "private"}


def _queue_success(
    context: _Context,
    *,
    checkin_id: object = "checkin-123",
    description: object = "连续签到 3 天",
    times: object = 5,
    rewards: object | None = None,
) -> None:
    if rewards is None:
        rewards = []
    context.queue(
        {"code": 0, "data": {"checkInId": checkin_id}},
        {
            "code": 0,
            "data": {"desc": description, "times": times, "list": rewards},
        },
    )


def test_help_describes_current_contract() -> None:
    help_text = signin._HELP_TEXT
    assert "/signin yingshi" in help_text
    assert "plugins.signin.yingshijufeng" in help_text


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "help", "帮助", "?"])
async def test_help_routes(args: str, context: _Context, event: dict[str, object]) -> None:
    result = await signin.handle("signin", args, event, context)

    assert "/signin yingshi" in text_segments_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["unknown", "sony", "s"])
async def test_unknown_platform_returns_help(
    target: str,
    context: _Context,
    event: dict[str, object],
) -> None:
    result = await signin.handle("signin", target, event, context)

    text = text_segments_text(result)
    assert f"未知平台: {target}" in text
    assert "/signin yingshi" in text
    assert context.raw_session.calls == []


@pytest.mark.asyncio
async def test_unknown_platform_echo_is_bounded(context: _Context) -> None:
    result = await signin.handle("signin", "x" * 500, {}, context)
    text = text_segments_text(result)
    visible = text.split("未知平台: ", 1)[1].split("\n", 1)[0]
    assert len(visible) <= 32
    assert "x" * 100 not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["yingshi", "yingshijufeng", "y"])
async def test_all_yingshi_aliases_execute_same_flow(
    alias: str,
    context: _Context,
    event: dict[str, object],
) -> None:
    _queue_success(context)

    result = await signin.handle("signin", alias, event, context)

    text = text_segments_text(result)
    assert "✅ 影视签到" in text
    assert "累计签到: 5 次" in text
    assert len(context.raw_session.calls) == 2


@pytest.mark.asyncio
async def test_success_formats_only_valid_reward_records(
    context: _Context,
    event: dict[str, object],
) -> None:
    _queue_success(
        context,
        rewards=[
            None,
            {"isSuccess": False, "times": "忽略"},
            {"isSuccess": True, "times": "第 5 天", "infos": "invalid"},
            {
                "isSuccess": True,
                "times": "第 6 天",
                "infos": {"title": "积分", "desc": "+10"},
            },
        ],
    )

    result = await signin.handle("signin", "yingshi", event, context)

    text = text_segments_text(result)
    assert "第 6 天: 积分 - +10" in text
    assert "🎁 奖励 第 5 天" in text
    assert ":  -" not in text
    assert "忽略" not in text


@pytest.mark.asyncio
async def test_missing_or_invalid_credentials_fail_before_http(event: dict[str, object]) -> None:
    context = _Context(configured=False)
    context.secrets["plugins"]["signin"]["yingshijufeng"] = {
        "app_id": "app",
        "kdt_id": "kdt",
        "access_token": {"nested": "invalid"},
        "sid": "sid",
    }

    result = await signin.handle("signin", "yingshi", event, context)

    assert "未配置" in text_segments_text(result)
    assert context.raw_session.calls == []


@pytest.mark.asyncio
async def test_missing_http_session_is_reported(event: dict[str, object]) -> None:
    context = _Context(with_http=False)

    result = await signin.handle("signin", "yingshi", event, context)

    assert "HTTP 会话未初始化" in text_segments_text(result)


@pytest.mark.asyncio
async def test_falsey_http_session_is_still_used(context: _Context) -> None:
    _queue_success(context)
    context.http_session = _FalseySession(context.http_session)

    result = await yingshi.yingshi_sign(context)

    assert "✅ 影视签到" in text_segments_text(result)


@pytest.mark.asyncio
async def test_query_and_checkin_provider_failures_are_reported(context: _Context) -> None:
    context.queue({"code": -1, "msg": "查询失败"})
    query_result = await yingshi.yingshi_sign(context)
    assert "查询失败" in text_segments_text(query_result)

    _queue_success(context)
    context.raw_session.responses[-1] = _Response({"code": -1, "msg": "今日已签到"})
    checkin_result = await yingshi.yingshi_sign(context)
    assert "今日已签到" in text_segments_text(checkin_result)


@pytest.mark.asyncio
async def test_structured_provider_message_uses_safe_fallback(context: _Context) -> None:
    context.queue({"code": -1, "msg": {"secret": "must-not-echo"}})

    result = await yingshi.yingshi_sign(context)

    text = text_segments_text(result)
    assert "获取签到信息失败" in text
    assert "must-not-echo" not in text


@pytest.mark.asyncio
async def test_query_response_requires_json_object(context: _Context) -> None:
    context.queue([])

    result = await yingshi.yingshi_sign(context)

    assert "XQ-PLUGIN-UNEXPECTED" in text_segments_text(result)
    assert len(context.raw_session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, [], "invalid"])
async def test_query_response_data_requires_mapping(
    data: object,
    context: _Context,
) -> None:
    context.queue({"code": 0, "data": data})

    ok, normalized, message = await yingshi._get_checkin_id(
        context.http_session,
        "app",
        "kdt",
        "token",
        {},
    )

    assert (ok, normalized, message) == (False, "", "获取签到信息失败")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkin_id", "expected_ok", "expected_id"),
    [
        ("cid", True, "cid"),
        (123, True, "123"),
        ("   ", False, ""),
        (True, False, ""),
        ([], False, ""),
    ],
)
async def test_checkin_id_requires_string_or_integer(
    checkin_id: object,
    expected_ok: bool,
    expected_id: str,
    context: _Context,
) -> None:
    context.queue({"code": 0, "data": {"checkInId": checkin_id}})

    ok, normalized, _message = await yingshi._get_checkin_id(
        context.http_session,
        "app",
        "kdt",
        "token",
        {},
    )

    assert ok is expected_ok
    assert normalized == expected_id


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, [], "invalid"])
async def test_checkin_response_requires_mapping(data: object, context: _Context) -> None:
    context.queue({"code": 0, "data": data})

    ok, message = await yingshi._do_checkin(
        context.http_session,
        "cid",
        "app",
        "kdt",
        "token",
        {},
    )

    assert ok is False
    assert message == "签到响应格式异常"


@pytest.mark.asyncio
async def test_provider_fields_and_rewards_are_bounded(context: _Context) -> None:
    long_text = "x" * 1000
    rewards = [
        None,
        {
            "isSuccess": True,
            "times": " ",
            "infos": {"title": "", "desc": ""},
        },
        *[
            {
                "isSuccess": True,
                "times": index,
                "infos": {"title": long_text, "desc": long_text},
            }
            for index in range(30)
        ],
    ]
    context.queue(
        {
            "code": 0,
            "data": {"desc": long_text, "times": 30, "list": rewards},
        }
    )

    ok, message = await yingshi._do_checkin(
        context.http_session,
        "cid",
        "app",
        "kdt",
        "token",
        {},
    )

    assert ok is True
    assert message.count("🎁") == yingshi._MAX_REWARD_LINES
    assert "…" in message
    assert len(message) < 12_000


@pytest.mark.asyncio
async def test_non_list_rewards_are_ignored(context: _Context) -> None:
    context.queue({"code": 0, "data": {"desc": "完成", "times": 1, "list": {}}})

    ok, message = await yingshi._do_checkin(
        context.http_session,
        "cid",
        "app",
        "kdt",
        "token",
        {},
    )

    assert ok is True
    assert "签到成功！完成" in message
    assert "🎁" not in message


def test_safe_text_uses_default_for_blank_scalar() -> None:
    assert yingshi._safe_text("   ", "fallback") == "fallback"


def test_headers_leave_transport_managed_fields_to_aiohttp() -> None:
    headers = yingshi._build_headers("wx12345")

    assert headers["content-type"] == "application/json"
    assert "wx12345" in headers["Referer"]
    assert "User-Agent" in headers
    assert "Host" not in headers
    assert "Connection" not in headers
    assert "Accept-Encoding" not in headers


def test_extra_data_uses_one_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yingshi.time, "time", lambda: 1234.567)

    extra = yingshi._build_extra_data("sid-value")

    assert extra["sid"] == "sid-value"
    assert extra["ftime"] == 1_234_567
    assert str(extra["uuid"]).endswith("1234567")


@pytest.mark.asyncio
async def test_token_uses_authorization_header_not_query(context: _Context) -> None:
    _queue_success(context)

    await yingshi.yingshi_sign(context)

    assert len(context.raw_session.calls) == 2
    for call in context.raw_session.calls:
        assert call["headers"]["Authorization"] == "Bearer test_access_token"
        assert "access_token" not in call["params"]
        assert "json" not in call


@pytest.mark.asyncio
async def test_scheduled_entry_delegates_to_signin(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    expected = [{"type": "text", "data": {"text": "scheduled"}}]
    sign = AsyncMock(return_value=expected)
    monkeypatch.setattr(signin.yingshi, "yingshi_sign", sign)

    assert await signin.scheduled_yingshi(context) == expected
    sign.assert_awaited_once_with(context)


def test_get_config_accepts_frozen_and_malformed_mappings(context: _Context) -> None:
    context.secrets = ConfigSnapshot(
        config={},
        secrets={
            "plugins": {
                "signin": {
                    "yingshijufeng": {
                        "app_id": "frozen-app",
                        "kdt_id": "frozen-kdt",
                    }
                }
            }
        },
    ).secrets
    frozen = yingshi._get_config(context)
    assert not isinstance(frozen, dict)
    assert frozen["app_id"] == "frozen-app"

    for secrets in (None, [], {"plugins": []}, {"plugins": {"signin": []}}):
        context.secrets = secrets
        assert yingshi._get_config(context) == {}


@pytest.mark.asyncio
async def test_unexpected_error_uses_public_error_boundary(
    monkeypatch: pytest.MonkeyPatch,
    context: _Context,
) -> None:
    monkeypatch.setattr(
        yingshi,
        "_get_checkin_id",
        AsyncMock(side_effect=RuntimeError("private upstream detail")),
    )

    result = await yingshi.yingshi_sign(context)

    text = text_segments_text(result)
    assert "XQ-PLUGIN-UNEXPECTED" in text
    assert "private upstream detail" not in text


def test_manifest_and_readme_match_runtime_contract() -> None:
    plugin_dir = ROOT / "plugins" / "signin"
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    command = manifest["commands"][0]
    schedule = manifest["schedule"][0]
    readme = (plugin_dir / "README.md").read_text(encoding="utf-8")

    assert manifest["entry"] == "main.py"
    assert (plugin_dir / manifest["entry"]).is_file()
    assert command["admin_only"] is True
    assert set(command["triggers"]) == {"signin", "签到"}
    assert manifest["concurrency"] == "sequential"
    assert schedule == {
        "id": "yingshi",
        "handler": "scheduled_yingshi",
        "cron": {"hour": 0, "minute": 30},
    }
    assert callable(getattr(signin, schedule["handler"]))
    for trigger in command["triggers"]:
        assert f"/{trigger}" in readme
    assert not (plugin_dir / "sony.py").exists()
    assert "sony" not in readme.casefold()
