# 验证各运行期 HTTP 调用点的响应预算与失败契约。

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from core.bounded_http import HttpStatusError, ResponseFormatError, ResponseLimitError
from core.onebot import OneBotHttpSender
from plugins.apod import main as apod
from plugins.signin import yingshi
from plugins.twitter import main as twitter
from plugins.wolframalpha import main as wolframalpha
from tests.helpers.http import QueuedAiohttpSession
from tests.helpers.settings_snapshot import with_settings_reader


class _AsyncContent:
    def __init__(self, chunks: list[bytes], *, fail_on_read: bool = False) -> None:
        self.chunks       = chunks
        self.fail_on_read = fail_on_read
        self.reads        = 0

    async def iter_chunked(self, _size: int):
        if self.fail_on_read:
            raise AssertionError("response body must not be read")
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int              = 200,
        content_type: str | None = "application/json",
        url: str                 = "https://provider.example/response",
        fail_on_read: bool       = False,
    ) -> None:
        self.status                  = status
        self.url                     = url
        self.headers: dict[str, str] = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        self.content = _AsyncContent([body], fail_on_read=fail_on_read)
        self.content_length = None
        self.closed         = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


def _json_response(payload, **kwargs) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"), **kwargs)


def _assert_transport_guards(call: tuple[str, str, dict]) -> None:
    kwargs = call[2]
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"


@pytest.mark.asyncio
async def test_yingshi_uses_bounded_json_and_transport_guards() -> None:
    session = QueuedAiohttpSession(_json_response({"code": 0, "data": {"checkInId": "cid"}}))

    ok, checkin_id, _message = await yingshi._get_checkin_id(
        session,
        "app",
        "kdt",
        "secret-token",
        {"Accept-Encoding": "br"},
    )

    assert (ok, checkin_id) == (True, "cid")
    _assert_transport_guards(session.requests[0])
    assert session.requests[0][2]["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_yingshi_rejects_mime_and_oversized_bodies() -> None:
    wrong_mime = QueuedAiohttpSession(
        _json_response(
            {"code": 0},
            content_type="text/html",
        )
    )
    with pytest.raises(ResponseFormatError):
        await yingshi._get_checkin_id(wrong_mime, "a", "k", "t", {})

    oversized = QueuedAiohttpSession(_Response(b"x" * (512 * 1024 + 1)))
    with pytest.raises(ResponseLimitError):
        await yingshi._get_checkin_id(oversized, "a", "k", "t", {})


@pytest.mark.asyncio
async def test_yingshi_non_success_does_not_read_error_body() -> None:
    response = _Response(
        b"sensitive upstream error" * 1000,
        status       = 503,
        fail_on_read = True,
    )
    with pytest.raises(HttpStatusError):
        await yingshi._get_checkin_id(QueuedAiohttpSession(response), "a", "k", "t", {})
    assert response.content.reads == 0
    assert response.closed is True


@pytest.mark.asyncio
async def test_twitter_non_success_does_not_read_body(tmp_path: Path) -> None:
    response = _Response(b"huge private error", status=429, fail_on_read=True)
    context = with_settings_reader(
        SimpleNamespace(
            http_session = QueuedAiohttpSession(response),
            data_dir     = tmp_path,
            secrets      = {"plugins": {"twitter": {"user_id": "123456789"}}},
        )
    )

    with pytest.raises(twitter.TwitterFetchError) as caught:
        await twitter._fetch_timeline(context)

    assert caught.value.status == 429
    assert response.content.reads == 0


@pytest.mark.asyncio
async def test_wolframalpha_rejects_entity_bearing_xml() -> None:
    session = QueuedAiohttpSession(
        _Response(
            b'<!DOCTYPE x [<!ENTITY payload "boom">]><queryresult>&payload;</queryresult>',
            content_type="application/xml",
        )
    )
    with pytest.raises(ResponseFormatError, match="DTD|entity"):
        await wolframalpha._query_step("x", "appid", session)


@pytest.mark.asyncio
async def test_onebot_private_provider_is_supported_and_bounded() -> None:
    session = QueuedAiohttpSession(
        _json_response(
            {"status": "ok", "retcode": 0, "data": {"message_id": 9}},
            content_type = None,
            url          = "http://127.0.0.1:5700/send_private_msg",
        )
    )
    sender = OneBotHttpSender("http://127.0.0.1:5700", "token", session)

    result = await sender.request_action(
        {"action": "send_private_msg", "params": {"user_id": 1, "message": []}}
    )

    assert result == {"status": "ok", "retcode": 0, "data": {"message_id": 9}}
    assert session.requests[0][1] == "http://127.0.0.1:5700/send_private_msg"
    _assert_transport_guards(session.requests[0])


@pytest.mark.asyncio
async def test_onebot_non_success_does_not_read_body() -> None:
    response = _Response(b"private failure", status=500, fail_on_read=True)
    sender = OneBotHttpSender(
        "http://onebot.internal:5700", "token", QueuedAiohttpSession(response)
    )

    assert await sender.request_action({"action": "get_status", "params": {}}) is None
    assert response.content.reads == 0


@pytest.mark.asyncio
async def test_apod_always_uses_pinned_public_fetch_for_configured_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from core.config import ConfigSnapshot

    html = (
        b"<html><center><b>APOD</b></center><iframe src='https://video.example/v'></iframe></html>"
    )
    fetch = AsyncMock(
        return_value=SimpleNamespace(
            url     = apod.DEFAULT_APOD_URL,
            status  = 200,
            body    = html,
            headers = {"Content-Type": "text/html"},
        )
    )
    monkeypatch.setattr(apod, "fetch_public_html", fetch)

    class _ExplodingSession:
        def request(self, *_args, **_kwargs):
            raise AssertionError("APOD must not use the injected raw session")

    context = with_settings_reader(
        SimpleNamespace(
            config=ConfigSnapshot(
                config={
                    "plugins": {
                        "apod": {
                            "url"          : apod.DEFAULT_APOD_URL,
                            "allowed_hosts": ["images.example"],
                        }
                    }
                },
                secrets={},
            ).config,
            data_dir     = tmp_path,
            http_session = _ExplodingSession(),
        )
    )

    result = await apod.handle("apod", "", {}, context)

    assert "https://video.example/v" in str(result)
    fetch.assert_awaited_once()
    assert {"images.example", urlsplit(apod.DEFAULT_APOD_URL).hostname} <= fetch.await_args.kwargs[
        "allowed_hosts"
    ]
    assert fetch.await_args.kwargs["allow_transparent_proxy_fake_dns"] is True
