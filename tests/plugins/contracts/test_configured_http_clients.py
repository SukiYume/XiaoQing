# 验证配置驱动的 HTTP 客户端共用有界请求接口。
from __future__ import annotations

import ast
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest

from core.ai import AIRequestError, complete_configured_route
from plugins.chat import main as chat
from plugins.voice import main as voice
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT         = REPOSITORY_ROOT
ERROR_CANARY = b"CR221_HUGE_PRIVATE_ERROR_BODY_CANARY"


def _core_ai_settings(api_base: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "ai": {
                "providers": {
                    "local": {
                        "api_base": api_base,
                        "endpoint_path": "/chat/completions",
                    }
                },
                "models": {
                    "local-model": {
                        "provider": "local",
                        "model": "local-model",
                        "modalities": ["text"],
                    }
                },
            },
            "plugins": {
                "ads_paper": {
                    "ai": {
                        "routes": {
                            "summary": {
                                "models": ["local-model"],
                                "temperature": 0.7,
                                "max_tokens": 1200,
                                "timeout_seconds": 60,
                                "max_retry": 0,
                            }
                        }
                    }
                },
                "pendo": {
                    "ai": {
                        "routes": {
                            "parse": {
                                "models": ["local-model"],
                                "temperature": 0.3,
                                "max_tokens": 1000,
                                "timeout_seconds": 17,
                                "max_retry": 0,
                            }
                        }
                    }
                },
                "xiaoqing_chat": {
                    "ai": {
                        "routes": {
                            "chat": {
                                "models": ["local-model"],
                                "temperature": 0.2,
                                "max_tokens": 50,
                                "timeout_seconds": 19,
                                "max_retry": 0,
                            }
                        }
                    }
                },
            },
        },
        {"ai": {"providers": {"local": {"api_key": "key"}}}},
    )


class _ChunkedContent:
    def __init__(self, chunks: list[bytes], *, poison: bool = False) -> None:
        self.chunks     = chunks
        self.poison     = poison
        self.iterations = 0

    async def iter_chunked(self, _chunk_bytes: int):
        self.iterations += 1
        if self.poison:
            raise AssertionError("non-success response body must not be read")
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(
        self,
        *,
        status: int,
        chunks: list[bytes],
        headers: dict[str, str] | None = None,
        poison: bool                   = False,
    ) -> None:
        self.status  = status
        self.headers = headers or {}
        self.content = _ChunkedContent(chunks, poison=poison)
        self.content_length = sum(len(chunk) for chunk in chunks)
        self.url            = ""
        self.closed         = False

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses                                    = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        response     = self.responses.pop(0)
        response.url = url
        return response


def _json_response(
    payload: Any,
    *,
    compressed: bool = False,
    split_at: int    = 7,
) -> _Response:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if compressed:
        body                        = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return _Response(
        status  = 200,
        chunks  = [body[:split_at], body[split_at:]],
        headers = headers,
    )


def _error_response(status: int = 500) -> _Response:
    return _Response(
        status  = status,
        chunks  = [ERROR_CANARY * 100_000],
        headers = {"Content-Type": "text/plain"},
        poison  = True,
    )


def _voice_context(tmp_path: Path, session: _Session) -> SimpleNamespace:
    return with_settings_reader(
        SimpleNamespace(
            secrets={
                "plugins": {
                    "voice": {
                        "subscription_key": "secret",
                        "region": "eastasia",
                        "voice_name": "zh-CN-XiaomoNeural",
                        "style": "cheerful",
                        "role": "Girl",
                        "proxy": "http://127.0.0.1:7890",
                    }
                }
            },
            data_dir     = tmp_path,
            http_session = session,
            logger       = MagicMock(),
        )
    )


@pytest.mark.asyncio
async def test_ads_llm_accepts_private_base_and_chunked_gzip() -> None:
    session = _Session(
        _json_response(
            {"choices": [{"message": {"content": "bounded summary"}}]},
            compressed=True,
        )
    )

    config, secrets = _core_ai_settings("http://127.0.0.1:18080/v1")
    result = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "ads_paper",
        route_name  = "summary",
        messages    = [{"role": "user", "content": "abstract"}],
    )

    assert result.content == "bounded summary"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "http://127.0.0.1:18080/v1/chat/completions")
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["timeout"].total == 60
    assert kwargs["headers"]["Accept-Encoding"] == "gzip, deflate"


@pytest.mark.asyncio
async def test_pendo_llm_preserves_private_base_proxy_timeout_and_no_redirect() -> None:
    session = _Session(
        _json_response(
            {"choices": [{"message": {"content": "local answer"}}]},
            compressed=True,
        )
    )

    config, secrets = _core_ai_settings("http://llm.internal:11434/v1")
    config["ai"]["providers"]["local"]["proxy"] = "http://127.0.0.1:7890"
    result                                      = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "pendo",
        route_name  = "parse",
        messages    = [{"role": "user", "content": "hello"}],
    )

    assert result.content == "local answer"
    _, url, kwargs = session.calls[0]
    assert url == "http://llm.internal:11434/v1/chat/completions"
    assert kwargs["proxy"] == "http://127.0.0.1:7890"
    assert kwargs["timeout"].total == 17
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False


@pytest.mark.asyncio
async def test_xiaoqing_llm_preserves_private_base_proxy_and_payload() -> None:
    session = _Session(
        _json_response(
            {"choices": [{"message": {"content": "ok"}}]},
            compressed=True,
        )
    )

    config, secrets = _core_ai_settings("http://127.0.0.1:11434/v1")
    config["ai"]["providers"]["local"]["proxy"] = "http://proxy.internal:8080"
    result                                      = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "xiaoqing_chat",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
    )

    assert result.content == "ok"
    _, url, kwargs = session.calls[0]
    assert url == "http://127.0.0.1:11434/v1/chat/completions"
    assert kwargs["proxy"] == "http://proxy.internal:8080"
    assert kwargs["timeout"].total == 19
    assert kwargs["json"]["stream"] is False
    assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_chat_preserves_proxy_and_parses_chunked_json() -> None:
    session = _Session(
        _json_response(
            {
                "data": {
                    "id": "chat-123",
                    "conversation_id": "conversation-456",
                    "status": "completed",
                }
            },
            compressed=True,
        ),
        _json_response(
            {"data": [{"type": "answer", "content": "coze answer"}]},
            compressed=True,
        ),
    )
    context = SimpleNamespace(http_session=session, logger=MagicMock())

    result = await chat.call_coze_api(
        "question",
        {"token": "token", "bot_id": "bot", "proxy": "http://127.0.0.1:7890"},
        context,
        actor_id="admin",
    )

    assert result == {"messages": [{"type": "answer", "content": "coze answer"}]}
    assert [call[1] for call in session.calls] == [chat.COZE_API_URL, chat.COZE_MESSAGES_URL]
    timeouts = [call[2]["timeout"] for call in session.calls]
    assert all(0 < timeout <= chat.REQUEST_TIMEOUT for timeout in timeouts)
    assert timeouts[1] <= timeouts[0]
    for _, _, kwargs in session.calls:
        assert kwargs["proxy"] == "http://127.0.0.1:7890"
        assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_voice_tts_uses_identity_existing_cap_and_proxy(tmp_path: Path) -> None:
    response = _Response(
        status  = 200,
        chunks  = [b"ID3audio", b"-data"],
        headers = {"Content-Type": "audio/mpeg"},
    )
    session = _Session(response)
    context = _voice_context(tmp_path, session)

    output = await voice.text_to_speech("bounded voice", context)

    assert output is not None
    assert Path(output).read_bytes() == b"ID3audio-data"
    _, _, kwargs = session.calls[0]
    assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert kwargs["proxy"] == "http://127.0.0.1:7890"
    assert kwargs["timeout"] is voice._AZURE_API_TIMEOUT
    assert voice._TTS_BODY_LIMITS.max_wire_bytes == voice.MAX_AUDIO_BYTES
    assert voice._TTS_BODY_LIMITS.max_decoded_bytes == voice.MAX_AUDIO_BYTES


@pytest.mark.asyncio
async def test_non_success_bodies_are_never_read_or_logged(tmp_path: Path) -> None:
    ads_response = _error_response(500)
    config, secrets = _core_ai_settings("http://127.0.0.1:18080/v1")
    with pytest.raises(AIRequestError, match="ai_server_error") as ads_error:
        await complete_configured_route(
            session     = _Session(ads_response),
            config      = config,
            secrets     = secrets,
            plugin_name = "ads_paper",
            route_name  = "summary",
            messages    = [{"role": "user", "content": "abstract"}],
        )
    assert ERROR_CANARY.decode() not in str(ads_error.value)

    xiaoqing_response = _error_response(400)
    with pytest.raises(AIRequestError, match="ai_invalid_request") as error:
        await complete_configured_route(
            session     = _Session(xiaoqing_response),
            config      = config,
            secrets     = secrets,
            plugin_name = "xiaoqing_chat",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "hello"}],
        )
    assert ERROR_CANARY.decode() not in str(error.value)

    chat_response = _error_response(401)
    chat_context = SimpleNamespace(http_session=_Session(chat_response), logger=MagicMock())
    assert (
        await chat.call_coze_api("question", {"token": "token", "bot_id": "bot"}, chat_context)
        is None
    )

    tts_response = _error_response(403)
    assert (
        await voice.text_to_speech(
            "error-body-canary", _voice_context(tmp_path, _Session(tts_response))
        )
        is None
    )

    for response in (
        ads_response,
        xiaoqing_response,
        chat_response,
        tts_response,
    ):
        assert response.content.iterations == 0


@pytest.mark.asyncio
async def test_llm_rejects_excessive_json_complexity() -> None:
    nested: Any = "leaf"
    for _ in range(40):
        nested = {"node": nested}
    response = _json_response(nested)

    config, secrets = _core_ai_settings("http://127.0.0.1:18080/v1")
    with pytest.raises(AIRequestError, match="ai_invalid_response"):
        await complete_configured_route(
            session     = _Session(response),
            config      = config,
            secrets     = secrets,
            plugin_name = "xiaoqing_chat",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "hello"}],
        )


@pytest.mark.asyncio
async def test_xiaoqing_transport_error_does_not_expose_private_provider() -> None:
    canary = "private-llm-canary.internal:18443"

    class FailingSession:
        def request(self, *_args: Any, **_kwargs: Any):
            raise aiohttp.ClientConnectionError(f"Cannot connect to host {canary}")

    config, secrets = _core_ai_settings(f"http://{canary}/v1")
    with pytest.raises(AIRequestError, match="^ai_transport$") as caught:
        await complete_configured_route(
            session     = FailingSession(),
            config      = config,
            secrets     = secrets,
            plugin_name = "xiaoqing_chat",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "hello"}],
        )

    assert canary not in str(caught.value)


@pytest.mark.asyncio
async def test_tts_declared_overflow_is_rejected_before_stream_read(tmp_path: Path) -> None:
    response = _Response(
        status  = 200,
        chunks  = [b"x"],
        headers = {
            "Content-Type": "audio/mpeg",
            "Content-Length": str(voice.MAX_AUDIO_BYTES + 1),
        },
        poison=True,
    )
    response.content_length = voice.MAX_AUDIO_BYTES + 1

    result = await voice.text_to_speech(
        "oversized-audio", _voice_context(tmp_path, _Session(response))
    )

    assert result is None
    assert response.content.iterations == 0


def test_configured_clients_forbid_direct_response_body_access() -> None:
    paths = (
        ROOT / "core" / "ai.py",
        ROOT / "plugins" / "chat" / "main.py",
        ROOT / "plugins" / "voice" / "main.py",
    )
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        atomic_store_variables = {
            target.id
            for assignment in ast.walk(tree)
            if isinstance(assignment, ast.Assign)
            and isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id == "AtomicJsonStore"
            for target in assignment.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"json", "text", "read"}:
                    if (
                        node.func.attr == "read"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in atomic_store_variables
                    ):
                        continue
                    violations.append(f"{path.name}:{node.lineno}: .{node.func.attr}()")
            if isinstance(node, ast.Attribute) and node.attr == "content":
                violations.append(f"{path.name}:{node.lineno}: .content")

    assert violations == []
