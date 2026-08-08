"""CR-221 bounded HTTP contracts for the remaining runtime clients."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.bounded_http import HttpStatusError, ResponseFormatError, ResponseLimitError
from core.onebot import OneBotHttpSender
from plugins.apod import main as apod
from plugins.signin import yingshi
from plugins.twitter import main as twitter
from plugins.wolframalpha import main as wolframalpha
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


class _AsyncContent:
    def __init__(self, chunks: list[bytes], *, fail_on_read: bool = False) -> None:
        self.chunks = chunks
        self.fail_on_read = fail_on_read
        self.reads = 0

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
        status: int = 200,
        content_type: str | None = "application/json",
        url: str = "https://provider.example/response",
        fail_on_read: bool = False,
    ) -> None:
        self.status = status
        self.url = url
        self.headers: dict[str, str] = {}
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        self.content = _AsyncContent([body], fail_on_read=fail_on_read)
        self.content_length = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _json_response(payload, **kwargs) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"), **kwargs)


def _assert_transport_guards(call: tuple[str, str, dict]) -> None:
    kwargs = call[2]
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"


@pytest.mark.asyncio
async def test_yingshi_uses_bounded_json_and_transport_guards() -> None:
    session = _Session(_json_response({"code": 0, "data": {"checkInId": "cid"}}))

    ok, checkin_id, _message = await yingshi._get_checkin_id(
        session,
        "app",
        "kdt",
        "secret-token",
        {"Accept-Encoding": "br"},
    )

    assert (ok, checkin_id) == (True, "cid")
    _assert_transport_guards(session.calls[0])
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_yingshi_rejects_mime_and_oversized_bodies() -> None:
    wrong_mime = _Session(
        _json_response(
            {"code": 0},
            content_type="text/html",
        )
    )
    with pytest.raises(ResponseFormatError):
        await yingshi._get_checkin_id(wrong_mime, "a", "k", "t", {})

    oversized = _Session(_Response(b"x" * (512 * 1024 + 1)))
    with pytest.raises(ResponseLimitError):
        await yingshi._get_checkin_id(oversized, "a", "k", "t", {})


@pytest.mark.asyncio
async def test_yingshi_non_success_does_not_read_error_body() -> None:
    response = _Response(
        b"sensitive upstream error" * 1000,
        status=503,
        fail_on_read=True,
    )
    with pytest.raises(HttpStatusError):
        await yingshi._get_checkin_id(_Session(response), "a", "k", "t", {})
    assert response.content.reads == 0
    assert response.closed is True


@pytest.mark.asyncio
async def test_twitter_preserves_explicit_proxy_and_bounds_timeline_json(tmp_path: Path) -> None:
    payload = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": [
                                        {"entryId": "tweet-1"},
                                        {
                                            "entryId": "cursor-bottom-1",
                                            "content": {"value": "next"},
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    session = _Session(_json_response(payload, url="https://x.com/api"))
    context = with_settings_reader(
        SimpleNamespace(
            http_session=session,
            data_dir=tmp_path,
            secrets={
                "plugins": {
                    "twitter": {
                        "user_id": "1",
                        "headers": {"author" + "ization": "Bearer token"},
                        "cookies": {"ct0": "csrf"},
                        "proxy": "http://127.0.0.1:7890",
                    }
                }
            },
        )
    )

    tweets, cursor, has_next = await twitter._fetch_timeline(context)

    assert [item["entryId"] for item in tweets] == ["tweet-1"]
    assert (cursor, has_next) == ("next", True)
    _assert_transport_guards(session.calls[0])
    kwargs = session.calls[0][2]
    assert kwargs["proxy"] == "http://127.0.0.1:7890"
    assert kwargs["cookies"] == {"ct0": "csrf"}


@pytest.mark.asyncio
async def test_twitter_non_success_does_not_read_body(tmp_path: Path) -> None:
    response = _Response(b"huge private error", status=429, fail_on_read=True)
    context = with_settings_reader(
        SimpleNamespace(
            http_session=_Session(response),
            data_dir=tmp_path,
            secrets={"plugins": {"twitter": {}}},
        )
    )

    with pytest.raises(twitter.TwitterFetchError) as caught:
        await twitter._fetch_timeline(context)

    assert caught.value.status == 429
    assert response.content.reads == 0


@pytest.mark.asyncio
async def test_wolframalpha_text_xml_and_json_use_bounded_contract() -> None:
    text_session = _Session(_Response(b"42", content_type="text/plain; charset=utf-8"))
    context = SimpleNamespace(http_session=text_session)
    assert "42" in str(await wolframalpha._get_answer("6*7", "appid", context))
    _assert_transport_guards(text_session.calls[0])

    xml_session = _Session(
        _Response(
            b"<queryresult><pod><plaintext>Step one</plaintext></pod></queryresult>",
            content_type="application/xml",
        )
    )
    assert "Step one" in await wolframalpha._query_step("x", "appid", xml_session)

    json_session = _Session(
        _json_response({"queryresult": {"pods": [{"subpods": [{"plaintext": "complete"}]}]}})
    )
    assert await wolframalpha._query_complete("x", "appid", json_session) == "complete"


@pytest.mark.asyncio
async def test_wolframalpha_rejects_entity_bearing_xml() -> None:
    session = _Session(
        _Response(
            b'<!DOCTYPE x [<!ENTITY payload "boom">]><queryresult>&payload;</queryresult>',
            content_type="application/xml",
        )
    )
    with pytest.raises(ResponseFormatError, match="DTD|entity"):
        await wolframalpha._query_step("x", "appid", session)


@pytest.mark.asyncio
async def test_onebot_private_provider_is_supported_and_bounded() -> None:
    session = _Session(
        _json_response(
            {"status": "ok", "retcode": 0, "data": {"message_id": 9}},
            content_type=None,
            url="http://127.0.0.1:5700/send_private_msg",
        )
    )
    sender = OneBotHttpSender("http://127.0.0.1:5700", "token", session)

    result = await sender.request_action(
        {"action": "send_private_msg", "params": {"user_id": 1, "message": []}}
    )

    assert result == {"status": "ok", "retcode": 0, "data": {"message_id": 9}}
    assert session.calls[0][1] == "http://127.0.0.1:5700/send_private_msg"
    _assert_transport_guards(session.calls[0])


@pytest.mark.asyncio
async def test_onebot_non_success_does_not_read_body() -> None:
    response = _Response(b"private failure", status=500, fail_on_read=True)
    sender = OneBotHttpSender("http://onebot.internal:5700", "token", _Session(response))

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
            url=apod.DEFAULT_APOD_URL,
            status=200,
            body=html,
            headers={"Content-Type": "text/html"},
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
                            "url": apod.DEFAULT_APOD_URL,
                            "allowed_hosts": ["images.example"],
                        }
                    }
                },
                secrets={},
            ).config,
            data_dir=tmp_path,
            http_session=_ExplodingSession(),
            logger=MagicMock(),
        )
    )

    result = await apod.handle("apod", "", {}, context)

    assert "https://video.example/v" in str(result)
    fetch.assert_awaited_once()
    assert fetch.await_args.kwargs["allowed_hosts"] == {"apod.nasa.gov", "images.example"}


@pytest.mark.parametrize(
    "relative_path",
    [
        "plugins/signin/yingshi.py",
        "plugins/twitter/main.py",
        "plugins/wolframalpha/main.py",
        "plugins/apod/main.py",
        "core/onebot.py",
    ],
)
def test_remaining_runtime_paths_have_no_direct_response_body_reads(
    relative_path: str,
) -> None:
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    forbidden = _raw_response_body_accesses(tree)
    assert forbidden == []


def _assigned_response_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for element in target.elts for name in _assigned_response_names(element)}
    return set()


def _is_transport_response(expression: ast.expr, aliases: set[str]) -> bool:
    if isinstance(expression, ast.Await):
        return _is_transport_response(expression.value, aliases)
    if isinstance(expression, ast.Name):
        return expression.id in aliases
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr in {"get", "post", "request"}
    )


def _raw_response_body_accesses(tree: ast.AST) -> list[tuple[int, str]]:
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            candidates: list[tuple[ast.expr, ast.expr]] = []
            if isinstance(node, ast.Assign):
                candidates.extend((target, node.value) for target in node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                candidates.append((node.target, node.value))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                candidates.extend(
                    (item.optional_vars, item.context_expr)
                    for item in node.items
                    if item.optional_vars is not None
                )
            for target, value in candidates:
                names = _assigned_response_names(target)
                if _is_transport_response(value, aliases) and not names <= aliases:
                    aliases.update(names)
                    changed = True

    forbidden: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"json", "text", "read", "content"}
            and _is_transport_response(node.value, aliases)
        ):
            forbidden.append((node.lineno, node.attr))
    return forbidden


def test_remaining_http_gate_follows_renamed_response_aliases() -> None:
    tree = ast.parse(
        """
async def bypass(client):
    pending = client.get("https://example.com")
    async with pending as payload_blob:
        copied = payload_blob
        return await copied.json()
"""
    )

    assert _raw_response_body_accesses(tree) == [(6, "json")]
