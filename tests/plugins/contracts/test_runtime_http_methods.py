"""CR-282 regressions for real imports and production HTTP boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.bounded_http import ResponseFormatError
from plugins.signin import yingshi
from plugins.twitter import main as twitter
from plugins.wolframalpha import main as wolframalpha
from tests.helpers.http import (
    QueuedAiohttpSession,
    aiohttp_json_response,
    aiohttp_text_response,
)
from tests.helpers.settings_snapshot import with_settings_reader


@pytest.mark.asyncio
async def test_twitter_uses_get_and_rejects_wrong_json_mime() -> None:
    response = aiohttp_json_response(
        {"data": {}},
        media_type="text/plain",
    )
    session = QueuedAiohttpSession(response)
    context = with_settings_reader(
        SimpleNamespace(
            http_session=session,
            secrets={"plugins": {"twitter": {}}},
            logger=MagicMock(),
        )
    )

    with pytest.raises(twitter.TwitterFetchError):
        await twitter._fetch_timeline(context)

    assert [call[0] for call in session.requests] == ["GET"]
    method, _, guarded_kwargs = session.requests[0]
    assert method == "GET"
    assert guarded_kwargs["allow_redirects"] is False
    assert guarded_kwargs["auto_decompress"] is False


@pytest.mark.asyncio
async def test_signin_uses_get_and_rejects_wrong_json_mime() -> None:
    response = aiohttp_json_response(
        {"code": 0, "data": {"checkInId": "cid"}},
        media_type="text/html",
    )
    session = QueuedAiohttpSession(response)

    with pytest.raises(ResponseFormatError, match="content type"):
        await yingshi._get_checkin_id(session, "app", "kdt", "token", {})

    assert [call[0] for call in session.requests] == ["GET"]


@pytest.mark.asyncio
async def test_wolfram_simple_query_uses_get() -> None:
    session = QueuedAiohttpSession(aiohttp_text_response("42"))
    context = SimpleNamespace(
        http_session=session,
        logger=MagicMock(),
    )

    result = await wolframalpha._get_answer("1+1", "appid", context)

    assert "42" in result[0]["data"]["text"]
    assert [call[0] for call in session.requests] == ["GET"]


@pytest.mark.asyncio
async def test_wolfram_get_redirect_is_rejected_and_closed() -> None:
    response = aiohttp_text_response(
        "redirect",
        status=302,
        headers={"Location": "https://example.com/elsewhere"},
    )
    session = QueuedAiohttpSession(response)
    context = SimpleNamespace(
        http_session=session,
        logger=MagicMock(),
    )

    result = await wolframalpha._get_answer("1+1", "appid", context)

    assert "XQ-PLUGIN-UNEXPECTED" in result[0]["data"]["text"]
    assert [call[0] for call in session.requests] == ["GET"]
    assert response.closed is True
