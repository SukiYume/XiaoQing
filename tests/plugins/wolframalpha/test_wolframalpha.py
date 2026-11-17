"""验证 Wolfram|Alpha 插件的命令、配置、解析和网络边界。"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import aiohttp
import pytest

from core.bounded_http import BoundedHttpResponse, HttpStatusError, ResponseFormatError
from core.public_errors import PUBLIC_ERROR_PREFIX
from plugins.wolframalpha import main as wolframalpha
from tests.helpers.assertions import logged_events
from tests.helpers.http import bounded_json_response
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


def _context(appid: object = "TEST-APPID") -> SimpleNamespace:
    """构造插件实际需要的最小上下文。"""

    return with_settings_reader(
        SimpleNamespace(
            secrets      = {"plugins": {"wolframalpha": {"appid": appid}}},
            http_session = object(),
        )
    )


def _response(
    body: bytes,
    media_type: str,
    *,
    charset: str | None = "utf-8",
) -> BoundedHttpResponse:
    return BoundedHttpResponse(
        url           = "https://api.wolframalpha.com/test",
        status        = 200,
        body          = body,
        media_type    = media_type,
        charset       = charset,
        headers       = {"Content-Type": media_type},
        wire_bytes    = len(body),
        decoded_bytes = len(body),
    )


class _BodyStream:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.body), max(1, size)):
            yield self.body[offset : offset + size]


class _RawResponse:
    """把有限响应数据暴露为 aiohttp 响应协议。"""

    def __init__(self, response: BoundedHttpResponse) -> None:
        self.status  = response.status
        self.url     = response.url
        content_type = response.media_type or "application/octet-stream"
        if response.charset is not None:
            content_type = f"{content_type}; charset={response.charset}"
        self.headers        = {"Content-Type": content_type}
        self.content_length = len(response.body)
        self.content        = _BodyStream(response.body)
        self.closed         = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


class _Session:
    """记录请求并依次返回协议正确的原始响应。"""

    def __init__(self, *responses: BoundedHttpResponse) -> None:
        self.responses                                       = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return _RawResponse(self.responses.pop(0))


def test_manifest_contract() -> None:
    manifest = json.loads(
        (ROOT / "plugins" / "wolframalpha" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "0.2.0"
    assert manifest["concurrency"] == "parallel"
    assert "schedule" not in manifest
    command = manifest["commands"][0]
    assert command["name"] == "alpha"
    assert command["triggers"] == ["alpha", "wolfram", "wa", "计算"]
    assert "admin_only" not in command
    assert command["usage"].startswith("/alpha")
    assert command["subcommands"][0]["name"] == "help"


def test_get_appid_reads_only_valid_secret_hierarchy() -> None:
    context = _context(" APP_123-xyz ")
    assert wolframalpha._get_appid(context) == "APP_123-xyz"
    boundary_value = "x" * wolframalpha.MAX_APPID_LENGTH
    assert wolframalpha._get_appid(_context(boundary_value)) == boundary_value

    for secrets in (None, [], {"plugins": []}, {"plugins": {"wolframalpha": []}}):
        context.secrets = secrets
        assert wolframalpha._get_appid(context) == ""


@pytest.mark.parametrize(
    "appid",
    [
        None,
        123,
        "",
        "with space",
        "bad\nvalue",
        "!invalid",
        "x" * (wolframalpha.MAX_APPID_LENGTH + 1),
    ],
    ids=["none", "non-string", "empty", "space", "control", "punctuation", "too-long"],
)
def test_get_appid_rejects_invalid_values(appid: object) -> None:
    assert wolframalpha._get_appid(_context(appid)) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "empty"),
        ("   ", "empty"),
        (" short ", "short"),
        ("x" * (wolframalpha.MAX_RESULT_TEXT_LENGTH + 1), "x" * 2399 + "…"),
    ],
)
def test_result_text_is_normalized_and_bounded(value: object, expected: str) -> None:
    assert wolframalpha._bound_result_text(value, empty_message="empty") == expected


def test_text_response_accepts_utf8_and_ascii_aliases() -> None:
    assert wolframalpha._decode_text_response(_response("答案".encode(), "text/plain")) == "答案"
    assert (
        wolframalpha._decode_text_response(_response(b"42", "text/plain", charset="US_ASCII"))
        == "42"
    )


@pytest.mark.parametrize(
    "response",
    [
        _response(b"42", "text/plain", charset="utf-16"),
        _response(b"\xff", "text/plain", charset="utf-8"),
    ],
    ids=["unsupported-charset", "invalid-utf8"],
)
def test_text_response_rejects_ambiguous_or_invalid_encoding(response) -> None:
    with pytest.raises(ResponseFormatError):
        wolframalpha._decode_text_response(response)


@pytest.mark.asyncio
async def test_handle_help_unknown_and_validation_errors() -> None:
    context = _context()
    assert "未知命令" in str(await wolframalpha.handle("other", "1+1", {}, context))
    assert "Wolfram|Alpha" in str(await wolframalpha.handle("alpha", "", {}, context))
    assert "Wolfram|Alpha" in str(await wolframalpha.handle("alpha", "--help", {}, context))
    assert "不接受额外参数" in str(await wolframalpha.handle("alpha", "--help extra", {}, context))
    assert "不支持的选项" in str(await wolframalpha.handle("alpha", "--z=1 --a=2 1+1", {}, context))
    assert "mode 仅支持" in str(
        await wolframalpha.handle("alpha", "--mode=invalid 1+1", {}, context)
    )
    assert "请输入问题" in str(await wolframalpha.handle("alpha", "--mode=step", {}, context))
    assert "查询过长" in str(
        await wolframalpha.handle(
            "alpha",
            "x" * (wolframalpha.MAX_QUERY_LENGTH + 1),
            {},
            context,
        )
    )
    assert "控制字符" in str(await wolframalpha.handle("alpha", "bad\0query", {}, context))
    assert "未配置有效 appid" in str(await wolframalpha.handle("alpha", "1+1", {}, _context("")))


def _answer_response(mode: str) -> BoundedHttpResponse:
    """各查询模式对应的 API 响应，答案文本都是 ``ok``。"""

    if mode == "step":
        return _response(
            b"<queryresult><pod><plaintext>ok</plaintext></pod></queryresult>",
            "application/xml",
        )
    if mode == "complete":
        return bounded_json_response(
            {"queryresult": {"pods": [{"subpods": [{"plaintext": "ok"}]}]}},
            url="https://api.wolframalpha.com/test",
        )
    return _response(b"ok", "text/plain")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "question", "mode"),
    [
        ("1+1", "1+1", "simple"),
        ("-1+2", "-1+2", "simple"),
        ("-3σ 偏差明显", "-3σ 偏差明显", "simple"),
        ("-- --mode=step", "--mode=step", "simple"),
        ("--mode=simple 1+1", "1+1", "simple"),
        ("--mode=step integrate x^2", "integrate x^2", "step"),
        ("--mode complete population of China", "population of China", "complete"),
        ("--mode=cp 6*7", "6*7", "complete"),
        ("help extra", "help extra", "simple"),
        ("帮助 extra", "帮助 extra", "simple"),
    ],
)
async def test_handle_passes_explicit_mode_and_question(
    args: str,
    question: str,
    mode: str,
) -> None:
    context              = _context()
    session              = _Session(_answer_response(mode))
    context.http_session = session

    assert "ok" in str(await wolframalpha.handle("alpha", args, {}, context))

    assert len(session.calls) == 1
    _method, url, kwargs = session.calls[0]
    params = kwargs["params"]
    assert params["appid"] == "TEST-APPID"
    if mode == "simple":
        assert url == wolframalpha.WA_RESULT_URL
        assert params["i"] == question
    else:
        assert url == wolframalpha.WA_QUERY_URL
        assert params["input"] == question
        assert ("podstate" in params) is (mode == "step")
        assert ("includepodid" in params) is (mode == "complete")


@pytest.mark.asyncio
async def test_handle_returns_public_error_for_unexpected_failure() -> None:
    context = _context()

    def broken_settings():
        raise RuntimeError("private settings detail")

    context.get_settings_snapshot = broken_settings
    result                        = await wolframalpha.handle("alpha", "1+1", {}, context)
    assert PUBLIC_ERROR_PREFIX in result[0]["data"]["text"]
    assert "private settings detail" not in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_simple_answer_uses_fixed_get_query_timeout_and_limits() -> None:
    context              = _context()
    session              = _Session(_response(b"42", "text/plain"))
    context.http_session = session

    result = await wolframalpha._get_answer("6*7", "APPID", context)

    assert "6*7" in str(result) and "42" in str(result)
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == wolframalpha.WA_RESULT_URL
    assert "APPID" not in url
    assert kwargs["params"] == {"appid": "APPID", "i": "6*7"}
    assert kwargs["timeout"] is wolframalpha._WA_TIMEOUT
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert wolframalpha._RESPONSE_LIMITS.max_wire_bytes == wolframalpha.MAX_RESPONSE_BYTES
    assert wolframalpha._RESPONSE_LIMITS.max_decoded_bytes == wolframalpha.MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_simple_answer_handles_empty_and_long_results() -> None:
    context              = _context()
    context.http_session = _Session(_response(b"   ", "text/plain"))
    assert "未找到结果" in str(await wolframalpha._get_answer("x", "APPID", context))

    context.http_session = _Session(
        _response(b"x" * (wolframalpha.MAX_RESULT_TEXT_LENGTH + 1), "text/plain"),
    )
    result = await wolframalpha._get_answer("x", "APPID", context)
    text   = result[0]["data"]["text"]
    assert text.endswith("…")
    assert len(text) < 3_000


@pytest.mark.asyncio
async def test_answer_runs_step_and_complete_queries_with_their_own_results() -> None:
    context = _context()
    context.http_session = session = _Session(
        _response(
            b"<queryresult><pod><plaintext>step result</plaintext></pod></queryresult>",
            "application/xml",
        ),
        bounded_json_response(
            {"queryresult": {"pods": [{"subpods": [{"plaintext": "complete result"}]}]}},
            url="https://api.wolframalpha.com/test",
        ),
    )

    step = str(await wolframalpha._get_answer("x", "APPID", context, mode="step"))
    complete = str(await wolframalpha._get_answer("x", "APPID", context, mode="complete"))

    assert "步骤解答" in step and "step result" in step
    assert "计算结果" in complete and "complete result" in complete
    assert [call[1] for call in session.calls] == [wolframalpha.WA_QUERY_URL] * 2


@pytest.mark.asyncio
async def test_answer_rejects_missing_http_session() -> None:
    context              = _context()
    context.http_session = None
    assert "HTTP 会话未初始化" in str(await wolframalpha._get_answer("1+1", "APPID", context))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "marker", "event", "level"),
    [
        (HttpStatusError(429), "HTTP 429", "query.http_error", logging.WARNING),
        (TimeoutError(), "查询超时", "query.timeout", logging.WARNING),
        (aiohttp.ClientError("network"), "网络错误", "query.network_error", logging.WARNING),
        (ResponseFormatError("bad"), "格式异常", "query.response_invalid", logging.WARNING),
        (RuntimeError("unexpected"), PUBLIC_ERROR_PREFIX, "public_error", logging.ERROR),
    ],
    ids=["http", "timeout", "network", "format", "unexpected"],
)
async def test_answer_maps_failures_to_stable_public_messages(
    monkeypatch,
    caplog,
    exception: Exception,
    marker: str,
    event: str,
    level: int,
) -> None:
    async def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(wolframalpha, "aiohttp_request_bounded", fail)
    with caplog.at_level(logging.WARNING):
        result = await wolframalpha._get_answer("1+1", "APPID", _context())
    assert marker in result[0]["data"]["text"]
    # 上游故障是已处理的降级，记 WARNING；只有意外异常才走 ERROR 级的 public_error。
    assert logged_events(caplog, event)
    assert max(record.levelno for record in caplog.records) == level
    if detail := str(exception):
        assert detail not in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_step_query_extracts_only_first_twenty_nonempty_items() -> None:
    items = ["<plaintext>   </plaintext>"] + [
        f"<plaintext>item-{index}</plaintext>" for index in range(21)
    ]
    session = _Session(
        _response(
            f"<queryresult><pod>{''.join(items)}</pod></queryresult>".encode(),
            "application/xml",
        ),
    )

    result = await wolframalpha._query_step("integrate x", "APPID", session)

    assert "item-0" in result and "item-19" in result
    assert "item-20" not in result
    assert session.calls[0][1] == wolframalpha.WA_QUERY_URL
    assert session.calls[0][2]["params"]["podstate"].startswith("Result__")


@pytest.mark.asyncio
async def test_step_query_returns_stable_empty_message() -> None:
    session = _Session(
        _response(b"<queryresult><pod /></queryresult>", "application/xml"),
    )
    assert await wolframalpha._query_step("x", "APPID", session) == "未找到步骤解答"


@pytest.mark.asyncio
async def test_complete_query_collects_valid_subpods_and_skips_bad_items() -> None:
    payload = {
        "queryresult": {
            "pods": [
                None,
                {"subpods": None},
                {"subpods": [None, {"plaintext": 1}, {"plaintext": " 42 "}]},
                {"subpods": [{"plaintext": "second"}]},
            ]
        }
    }
    session = _Session(bounded_json_response(payload, url="https://api.wolframalpha.com/test"))

    result = await wolframalpha._query_complete("6*7", "APPID", session)

    assert result == "42\n\nsecond"
    request_params = session.calls[0][2]["params"]
    assert request_params["includepodid"] == "Result"
    assert request_params["output"] == "json"


@pytest.mark.asyncio
async def test_complete_query_stops_after_twenty_results() -> None:
    subpods = [{"plaintext": f"item-{index}"} for index in range(21)]
    session = _Session(
        bounded_json_response(
            {"queryresult": {"pods": [{"subpods": subpods}]}},
            url="https://api.wolframalpha.com/test",
        ),
    )

    result = await wolframalpha._query_complete("x", "APPID", session)

    assert "item-19" in result
    assert "item-20" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"queryresult": {}}, "结果解析失败"),
        ({"queryresult": {"pods": []}}, "未找到结果"),
        ({"queryresult": {"pods": [None, {"subpods": None}]}}, "未找到结果"),
    ],
)
async def test_complete_query_handles_missing_or_empty_structure(
    payload: object,
    expected: str,
) -> None:
    session = _Session(bounded_json_response(payload, url="https://api.wolframalpha.com/test"))
    assert await wolframalpha._query_complete("x", "APPID", session) == expected


@pytest.mark.asyncio
async def test_complete_query_rejects_non_object_root() -> None:
    session = _Session(bounded_json_response([], url="https://api.wolframalpha.com/test"))
    with pytest.raises(ResponseFormatError):
        await wolframalpha._query_complete("x", "APPID", session)
