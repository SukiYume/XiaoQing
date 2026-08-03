"""CR-282 regressions for real imports and production HTTP boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.bounded_http import ResponseFormatError
from plugins.signin import yingshi
from plugins.twitter import main as twitter
from plugins.wolframalpha import main as wolframalpha
from tests.aiohttp_fakes import wrap_legacy_aiohttp_session
from tests.helpers.settings_snapshot import with_settings_reader


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        payload: dict | None = None,
        text: str = "",
        content_type: str,
        location: str | None = None,
    ):
        self.status = status
        self._json_data = payload if payload is not None else None
        self._text = text
        self.headers = {"Content-Type": content_type}
        if location is not None:
            self.headers["Location"] = location
        self.closed = False

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self):
        self.closed = True


class _MethodSession:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_twitter_uses_get_and_rejects_wrong_json_mime() -> None:
    response = _Response(
        payload={"data": {}},
        content_type="text/plain",
    )
    raw_session = _MethodSession(response)
    session = wrap_legacy_aiohttp_session(raw_session)
    context = with_settings_reader(
        SimpleNamespace(
            http_session=session,
            secrets={"plugins": {"twitter": {}}},
            logger=MagicMock(),
        )
    )

    with pytest.raises(twitter.TwitterFetchError):
        await twitter._fetch_timeline(context)

    assert [call[0] for call in raw_session.calls] == ["GET"]
    method, _, guarded_kwargs = session.requests[0]
    assert method == "GET"
    assert guarded_kwargs["allow_redirects"] is False
    assert guarded_kwargs["auto_decompress"] is False


@pytest.mark.asyncio
async def test_signin_uses_get_and_rejects_wrong_json_mime() -> None:
    response = _Response(
        payload={"code": 0, "data": {"checkInId": "cid"}},
        content_type="text/html",
    )
    raw_session = _MethodSession(response)
    session = wrap_legacy_aiohttp_session(raw_session)

    with pytest.raises(ResponseFormatError, match="content type"):
        await yingshi._get_checkin_id(session, "app", "kdt", "token", {})

    assert [call[0] for call in raw_session.calls] == ["GET"]


@pytest.mark.asyncio
async def test_wolfram_simple_query_uses_get() -> None:
    response = _Response(text="42", content_type="text/plain")
    del response._json_data

    class GetOnlySession:
        def __init__(self):
            self.get_calls = 0

        def get(self, *_args, **_kwargs):
            self.get_calls += 1
            return response

    raw_session = GetOnlySession()
    context = SimpleNamespace(
        http_session=wrap_legacy_aiohttp_session(raw_session),
        logger=MagicMock(),
    )

    result = await wolframalpha._get_answer("1+1", "appid", context)

    assert "42" in result[0]["data"]["text"]
    assert raw_session.get_calls == 1


@pytest.mark.asyncio
async def test_wolfram_get_redirect_is_rejected_and_closed() -> None:
    response = _Response(
        status=302,
        text="redirect",
        content_type="text/plain",
        location="https://example.com/elsewhere",
    )
    raw_session = _MethodSession(response)
    context = SimpleNamespace(
        http_session=wrap_legacy_aiohttp_session(raw_session),
        logger=MagicMock(),
    )

    result = await wolframalpha._get_answer("1+1", "appid", context)

    assert "XQ-PLUGIN-UNEXPECTED" in result[0]["data"]["text"]
    assert [call[0] for call in raw_session.calls] == ["GET"]
    assert response.closed is True
