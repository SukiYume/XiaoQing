# 验证统一 AI 服务的配置路由、模型调用与失败边界。
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.ai import (
    AIConfigError,
    AIRequestError,
    complete_configured_route,
    list_configured_models,
)


class _Body:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.body), max(1, size)):
            yield self.body[offset : offset + size]


class _Response:
    def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
        body         = json.dumps(payload or {}).encode("utf-8")
        self.status  = status
        self.url     = ""
        self.headers = {
            "Content-Type"  : "application/json",
            "Content-Length": str(len(body)),
        }
        self.content_length = len(body)
        self.content        = _Body(body)
        self.closed         = False

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


class _Session:
    closed = False

    def __init__(self, *responses: _Response) -> None:
        self.responses                                    = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        response     = self.responses.pop(0)
        response.url = url
        return response


def _completion(content: str = "ok", *, model: str = "remote-model") -> dict[str, Any]:
    return {
        "model"  : model,
        "choices": [
            {
                "message"      : {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _settings() -> tuple[dict[str, Any], dict[str, Any]]:
    config = {
        "ai": {
            "providers": {
                "primary": {
                    "api_base"     : "https://primary.example/v1",
                    "endpoint_path": "/chat/completions",
                    "proxy"        : "http://proxy.example:8080",
                },
                "backup": {
                    "api_base"     : "https://backup.example/api",
                    "endpoint_path": "/chat/completions",
                },
            },
            "models": {
                "primary-text": {
                    "provider"        : "primary",
                    "model"           : "primary-model",
                    "modalities"      : ["text"],
                    "request_defaults": {
                        "thinking": {"type": "disabled"},
                    },
                },
                "backup-text": {
                    "provider"  : "backup",
                    "model"     : "backup-model",
                    "modalities": ["text"],
                },
                "backup-vision": {
                    "provider"  : "backup",
                    "model"     : "vision-model",
                    "modalities": ["text", "image"],
                },
            },
        },
        "plugins": {
            "demo": {
                "ai": {
                    "routes": {
                        "chat": {
                            "models"                : ["primary-text", "backup-text"],
                            "temperature"           : 0.4,
                            "max_tokens"            : 128,
                            "timeout_seconds"       : 5,
                            "total_timeout_seconds" : 15,
                            "max_retry"             : 0,
                            "retry_interval_seconds": 0,
                            "request_defaults"      : {
                                "response_format": {"type": "json_object"},
                            },
                        },
                        "vision": {
                            "models"   : ["backup-vision"],
                            "max_retry": 0,
                        },
                    }
                }
            }
        },
    }
    secrets = {
        "ai": {
            "providers": {
                "primary": {"api_key": "<PRIMARY_API_KEY>"},
                "backup" : {"api_key": "<BACKUP_API_KEY>"},
            }
        }
    }
    return config, secrets


def test_ai_model_listing_preserves_order_without_exposing_credentials() -> None:
    config, secrets = _settings()

    models = list_configured_models(
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
    )

    assert [model.name for model in models] == ["primary-text", "backup-text"]
    assert [model.provider for model in models] == ["primary", "backup"]
    assert all(not hasattr(model, "api_key") for model in models)


@pytest.mark.asyncio
async def test_gpt_6_luna_preserves_sampling_with_reasoning_disabled() -> None:
    config, secrets = _settings()
    model                     = config["ai"]["models"]["primary-text"]
    model["model"]            = "gpt-6-luna"
    model["request_defaults"] = {"reasoning_effort": "none"}
    session                   = _Session(_Response(200, _completion()))
    await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
        max_tokens  = 512,
        top_p       = 0.9,
    )
    payload = session.calls[0][2]["json"]
    assert payload["max_completion_tokens"] == 512
    assert "max_tokens" not in payload
    assert payload["reasoning_effort"] == "none"
    assert payload["temperature"] == 0.4
    assert payload["top_p"] == 0.9


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, 512])
async def test_chat_latest_uses_supported_sampling_and_completion_budget(budget) -> None:
    config, secrets = _settings()
    config["ai"]["models"]["primary-text"]["model"] = "chat-latest"
    session                                         = _Session(_Response(200, _completion()))
    await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
        max_tokens  = budget,
        top_p       = 0.9,
    )
    payload = session.calls[0][2]["json"]
    assert payload["max_completion_tokens"] == (128 if budget is None else budget)
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload


@pytest.mark.asyncio
async def test_ai_route_uses_first_model_and_merges_explicit_request_layers() -> None:
    config, secrets = _settings()
    session = _Session(_Response(200, _completion()))

    result = await complete_configured_route(
        session       = session,
        config        = config,
        secrets       = secrets,
        plugin_name   = "demo",
        route_name    = "chat",
        messages      = [{"role": "user", "content": "hello"}],
        top_p         = 0.9,
        extra_payload = {"seed": 7},
    )

    assert result.content == "ok"
    assert result.profile == "primary-text"
    assert result.provider == "primary"
    assert result.attempts == 1
    method, url, kwargs = session.calls[0]
    assert (method, url) == (
        "POST",
        "https://primary.example/v1/chat/completions",
    )
    assert kwargs["proxy"] == "http://proxy.example:8080"
    assert kwargs["headers"]["Authorization"] == "Bearer <PRIMARY_API_KEY>"
    assert kwargs["json"] == {
        "model"          : "primary-model",
        "messages"       : [{"role": "user", "content": "hello"}],
        "stream"         : False,
        "thinking"       : {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature"    : 0.4,
        "top_p"          : 0.9,
        "max_tokens"     : 128,
        "seed"           : 7,
    }


@pytest.mark.asyncio
async def test_ai_route_falls_back_in_order_after_rate_limit() -> None:
    config, secrets = _settings()
    session = _Session(
        _Response(429),
        _Response(200, _completion("backup", model="backup-model")),
    )

    result = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
    )

    assert result.content == "backup"
    assert result.profile == "backup-text"
    assert result.attempts == 2
    assert [call[1] for call in session.calls] == [
        "https://primary.example/v1/chat/completions",
        "https://backup.example/api/chat/completions",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": [{}]},
        {"role": "assistant", "content": [{"text": ""}]},
        {"role": "assistant", "content": [{"text": "   "}]},
        {"role": "assistant", "content": [None]},
        {"role": "assistant", "content": None, "tool_calls": [{}]},
    ],
)
async def test_ai_route_treats_structurally_nonempty_but_semantically_empty_parts_as_empty(
    message: dict[str, Any],
) -> None:
    config, secrets = _settings()
    empty_completion = {
        "choices": [
            {
                "message"      : message,
                "finish_reason": "stop",
            }
        ]
    }
    session = _Session(
        _Response(200, empty_completion),
        _Response(200, _completion("backup")),
    )

    result = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
    )

    assert result.content == "backup"
    assert result.profile == "backup-text"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_ai_route_accepts_text_from_structured_content_parts() -> None:
    config, secrets = _settings()
    session = _Session(
        _Response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role"   : "assistant",
                            "content": [{"type": "text", "text": "first"}, {"text": "second"}],
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )

    result = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
    )

    assert result.content == "first\nsecond"


@pytest.mark.asyncio
async def test_ai_route_retries_a_model_before_using_the_next_profile() -> None:
    config, secrets = _settings()
    config["plugins"]["demo"]["ai"]["routes"]["chat"]["max_retry"] = 1
    session                                                        = _Session(
        _Response(500),
        _Response(500),
        _Response(200, _completion("backup")),
    )

    result = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hello"}],
    )

    assert result.profile == "backup-text"
    assert result.attempts == 3
    assert [call[1] for call in session.calls] == [
        "https://primary.example/v1/chat/completions",
        "https://primary.example/v1/chat/completions",
        "https://backup.example/api/chat/completions",
    ]


@pytest.mark.asyncio
async def test_ai_route_does_not_hide_authentication_failure_with_fallback() -> None:
    config, secrets = _settings()
    session = _Session(
        _Response(401),
        _Response(200, _completion("must-not-run")),
    )

    with pytest.raises(AIRequestError, match="ai_authentication") as captured:
        await complete_configured_route(
            session     = session,
            config      = config,
            secrets     = secrets,
            plugin_name = "demo",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "hello"}],
        )

    assert captured.value.status == 401
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_ai_pinned_model_is_strict_and_skips_other_profiles() -> None:
    config, secrets = _settings()
    session = _Session(_Response(200, _completion("pinned")))

    result = await complete_configured_route(
        session      = session,
        config       = config,
        secrets      = secrets,
        plugin_name  = "demo",
        route_name   = "chat",
        pinned_model = "backup-text",
        messages     = [{"role": "user", "content": "hello"}],
    )

    assert result.content == "pinned"
    assert result.profile == "backup-text"
    assert session.calls[0][1] == "https://backup.example/api/chat/completions"


@pytest.mark.asyncio
async def test_ai_route_rejects_modality_mismatch_before_network() -> None:
    config, secrets = _settings()
    session = _Session()

    with pytest.raises(AIConfigError, match="does not support"):
        await complete_configured_route(
            session             = session,
            config              = config,
            secrets             = secrets,
            plugin_name         = "demo",
            route_name          = "chat",
            required_modalities = ("text", "image"),
            messages            = [{"role": "user", "content": "image"}],
        )

    assert session.calls == []


def test_ai_route_requires_valid_references_and_provider_credentials() -> None:
    config, secrets = _settings()
    config["plugins"]["demo"]["ai"]["routes"]["chat"]["models"] = ["missing"]

    with pytest.raises(AIConfigError, match="config.ai.models.missing"):
        list_configured_models(
            config      = config,
            secrets     = secrets,
            plugin_name = "demo",
            route_name  = "chat",
        )

    config, secrets = _settings()
    del secrets["ai"]["providers"]["primary"]["api_key"]
    models = list_configured_models(
        config=config, secrets=secrets, plugin_name="demo", route_name="chat"
    )
    assert [model.name for model in models] == ["backup-text"]


def test_ai_route_rejects_reserved_payload_overrides() -> None:
    config, secrets = _settings()
    config["ai"]["models"]["primary-text"]["request_defaults"] = {"model": "injected"}

    with pytest.raises(AIConfigError, match="reserved"):
        list_configured_models(
            config      = config,
            secrets     = secrets,
            plugin_name = "demo",
            route_name  = "chat",
        )

    config, secrets = _settings()
    config["plugins"]["demo"]["ai"]["routes"]["chat"]["request_defaults"] = {"temperature": 1.9}
    with pytest.raises(AIConfigError, match="reserved"):
        list_configured_models(
            config      = config,
            secrets     = secrets,
            plugin_name = "demo",
            route_name  = "chat",
        )


@pytest.mark.asyncio
async def test_ai_request_rejects_reserved_extra_payload_before_network() -> None:
    config, secrets = _settings()
    session = _Session()

    with pytest.raises(AIConfigError, match="reserved"):
        await complete_configured_route(
            session       = session,
            config        = config,
            secrets       = secrets,
            plugin_name   = "demo",
            route_name    = "chat",
            messages      = [{"role": "user", "content": "hello"}],
            extra_payload = {"model": "must-not-override"},
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_ai_route_total_timeout_bounds_response_reading() -> None:
    class _SlowBody(_Body):
        async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
            await asyncio.sleep(1)
            async for chunk in super().iter_chunked(size):
                yield chunk

    config, secrets = _settings()
    config["plugins"]["demo"]["ai"]["routes"]["chat"]["total_timeout_seconds"] = 0.1
    response = _Response(200, _completion())
    response.content = _SlowBody(response.content.body)
    session = _Session(response)

    with pytest.raises(AIRequestError, match="ai_route_timeout"):
        await complete_configured_route(
            session     = session,
            config      = config,
            secrets     = secrets,
            plugin_name = "demo",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "hello"}],
        )

    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["provider", "key", "empty", "whitespace"])
@pytest.mark.parametrize("provider", ["primary", "backup"])
async def test_ai_skips_missing_credentials_before_requests(provider: str, missing: str) -> None:
    config, secrets = _settings()
    providers = secrets["ai"]["providers"]
    if missing == "provider":
        del providers[provider]
    elif missing == "key":
        providers[provider] = {}
    else:
        providers[provider]["api_key"] = "" if missing == "empty" else "   "
    expected = "backup" if provider == "primary" else "primary"
    models   = list_configured_models(
        config=config, secrets=secrets, plugin_name="demo", route_name="chat"
    )
    assert [model.provider for model in models] == [expected]
    session = _Session(_Response(200, _completion()))
    result  = await complete_configured_route(
        session     = session,
        config      = config,
        secrets     = secrets,
        plugin_name = "demo",
        route_name  = "chat",
        messages    = [{"role": "user", "content": "hi"}],
    )
    assert result.provider == expected
    assert len(session.calls) == 1
    assert f"{expected}.example" in session.calls[0][1]


@pytest.mark.asyncio
async def test_ai_missing_pinned_key_and_empty_chain_fail_before_network() -> None:
    config, secrets = _settings()
    secrets["ai"]["providers"]["primary"]["api_key"] = ""
    session                                          = _Session()
    with pytest.raises(AIConfigError, match="pinned model.*missing API key"):
        await complete_configured_route(
            session      = session,
            config       = config,
            secrets      = secrets,
            plugin_name  = "demo",
            route_name   = "chat",
            pinned_model = "primary-text",
            messages     = [{"role": "user", "content": "hi"}],
        )
    with pytest.raises(AIConfigError, match="no models with an API key"):
        await complete_configured_route(
            session     = session,
            config      = config,
            secrets     = {},
            plugin_name = "demo",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "hi"}],
        )
    assert session.calls == []


@pytest.mark.parametrize("broken", ["key_type", "url", "model_defaults"])
def test_ai_missing_key_preserves_structural_validation(broken: str) -> None:
    config, secrets = _settings()
    secrets["ai"]["providers"]["primary"]["api_key"] = ""
    if broken == "key_type":
        secrets["ai"]["providers"]["primary"]["api_key"] = 123
    elif broken == "url":
        config["ai"]["providers"]["primary"]["api_base"] = "invalid-url"
    else:
        config["ai"]["models"]["primary-text"]["request_defaults"] = {"model": "other"}
    with pytest.raises(AIConfigError):
        list_configured_models(
            config=config, secrets=secrets, plugin_name="demo", route_name="chat"
        )


@pytest.mark.asyncio
async def test_ai_route_settings_reach_payload_and_log_without_credentials(caplog) -> None:
    config, secrets = _settings()
    route = config["plugins"]["demo"]["ai"]["routes"]["chat"]
    route.update(
        temperature            = 0.23,
        top_p                  = 0.67,
        max_tokens             = 1456,
        timeout_seconds        = 9,
        total_timeout_seconds  = 25,
        max_retry              = 2,
        retry_interval_seconds = 0.75,
    )
    session = _Session(_Response(200, _completion()))
    with caplog.at_level("DEBUG", logger="core.ai"):
        await complete_configured_route(
            session     = session,
            config      = config,
            secrets     = secrets,
            plugin_name = "demo",
            route_name  = "chat",
            messages    = [{"role": "user", "content": "private message"}],
        )
    request = session.calls[0][2]
    assert request["json"]["temperature"] == 0.23
    assert request["json"]["top_p"] == 0.67
    assert request["json"]["max_tokens"] == 1456
    assert request["timeout"].total == 9
    assert "max_retry=2" in caplog.text
    assert "<PRIMARY_API_KEY>" not in caplog.text
    assert "private message" not in caplog.text
