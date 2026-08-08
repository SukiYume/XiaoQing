"""验证 Voice 插件的配置、TTS 音频边界、缓存和命令契约。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.bounded_http import BoundedHttpResponse, HttpStatusError
from plugins.voice import main as voice
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT
VALID_CONFIG = {
    "subscription_key": "test-key",
    "region": "southeastasia",
    "voice_name": "zh-CN-XiaomoNeural",
    "style": "cheerful",
    "role": "Girl",
    "proxy": "",
}


def _context(tmp_path: Path, config: object = VALID_CONFIG) -> SimpleNamespace:
    """构造只含 Voice 实际读取字段的测试上下文。"""

    return with_settings_reader(
        SimpleNamespace(
            data_dir=tmp_path / "voice-data",
            http_session=object(),
            logger=MagicMock(),
            secrets={"plugins": {"voice": config}},
        )
    )


def _response(body: bytes, media_type: str) -> BoundedHttpResponse:
    return BoundedHttpResponse(
        url="https://voice.test/",
        status=200,
        body=body,
        media_type=media_type,
        charset="utf-8" if media_type == "application/json" else None,
        headers={"Content-Type": media_type},
        wire_bytes=len(body),
        decoded_bytes=len(body),
    )


def _install_response(
    monkeypatch: pytest.MonkeyPatch,
    response: BoundedHttpResponse,
) -> list[tuple[object, str, str, dict[str, object]]]:
    calls: list[tuple[object, str, str, dict[str, object]]] = []

    async def fake_request(session, method, url, **kwargs):
        calls.append((session, method, url, kwargs))
        return response

    monkeypatch.setattr(voice, "aiohttp_request_bounded", fake_request)
    return calls


def test_static_metadata() -> None:
    assert "语音" in voice._HELP_TEXT

    manifest = json.loads((ROOT / "plugins" / "voice" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["concurrency"] == "parallel"
    command = manifest["commands"][0]
    assert command["name"] == "tts"
    assert command["triggers"] == ["语音", "念", "tts"]
    assert command["admin_only"] is True
    assert command["usage"] == "/语音 <文本>"
    assert command["subcommands"][0]["name"] == "help"
    assert manifest["services"][0]["callback"] == "convert_text_to_voice"


def test_settings_reject_malformed_layers_and_missing_key(tmp_path: Path) -> None:
    context = _context(tmp_path)
    for secrets in (None, [], {"plugins": []}, {"plugins": {"voice": []}}):
        context.secrets = secrets
        assert voice._get_settings(context) is None

    context.secrets = {"plugins": {"voice": {"subscription_key": " \n "}}}
    assert voice._get_settings(context) is None


def test_settings_use_safe_defaults_and_validate_proxy(tmp_path: Path) -> None:
    config = {
        "subscription_key": " key ",
        "region": "../../evil",
        "voice_name": "bad voice",
        "style": "bad<style",
        "role": 42,
        "proxy": "https://proxy.test:8443/",
    }
    settings = voice._get_settings(_context(tmp_path, config))

    assert settings is not None
    assert settings.subscription_key == "key"
    assert settings.region == voice.DEFAULT_REGION
    assert settings.voice_name == voice.DEFAULT_VOICE_NAME
    assert settings.style == voice.DEFAULT_STYLE
    assert settings.role == voice.DEFAULT_ROLE
    assert settings.proxy == "https://proxy.test:8443/"


@pytest.mark.parametrize(
    "proxy",
    [
        123,
        "",
        "ftp://proxy.test",
        "https://",
        "https://proxy.test/path",
        "https://proxy.test?key=value",
        "https://proxy.test/#fragment",
        "https://proxy.test:70000",
        "https://proxy.test\n",
        "x" * (voice.MAX_PROXY_LENGTH + 1),
    ],
    ids=[
        "non-string",
        "empty",
        "wrong-scheme",
        "missing-host",
        "path",
        "query",
        "fragment",
        "bad-port",
        "control-char",
        "too-long",
    ],
)
def test_settings_ignore_invalid_proxy(tmp_path: Path, proxy: object) -> None:
    config = {**VALID_CONFIG, "proxy": proxy}
    settings = voice._get_settings(_context(tmp_path, config))
    assert settings is not None
    assert settings.proxy is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(b"ID3audio", True), (b"\xff\xfbaudio", True), (b"<html>", False), (b"", False)],
)
def test_mp3_header_detection(payload: bytes, expected: bool) -> None:
    assert voice._looks_like_mp3(payload) is expected


@pytest.mark.asyncio
async def test_tts_builds_escaped_bounded_request(monkeypatch, tmp_path: Path) -> None:
    config = {**VALID_CONFIG, "proxy": "http://127.0.0.1:7890"}
    context = _context(tmp_path, config)
    calls = _install_response(monkeypatch, _response(b"ID3audio", "audio/mpeg"))

    output = await voice.text_to_speech("  <你好&  ", context)

    assert output is not None
    assert Path(output).read_bytes() == b"ID3audio"
    session, method, url, kwargs = calls[0]
    assert session is context.http_session
    assert method == "POST"
    assert url.startswith("https://southeastasia.tts.speech.microsoft.com/")
    assert kwargs["accept_encoding"] == "identity"
    assert kwargs["limits"] is voice._TTS_BODY_LIMITS
    assert kwargs["mime_policy"] is voice._TTS_MIME
    request_kwargs = kwargs["request_kwargs"]
    assert request_kwargs["proxy"] == "http://127.0.0.1:7890"
    assert request_kwargs["timeout"] is voice._AZURE_API_TIMEOUT
    assert b"&lt;\xe4\xbd\xa0\xe5\xa5\xbd&amp;" in request_kwargs["data"]


@pytest.mark.asyncio
async def test_tts_cache_reuses_valid_audio_and_separates_voice_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, dict(VALID_CONFIG))
    calls = _install_response(monkeypatch, _response(b"ID3cached", "audio/mpeg"))

    first = await voice.text_to_speech("同一句话", context)
    second = await voice.text_to_speech("同一句话", context)
    context.secrets["plugins"]["voice"]["style"] = "sad"
    third = await voice.text_to_speech("同一句话", context)

    assert first == second
    assert first != third
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_tts_replaces_corrupt_legacy_cache(monkeypatch, tmp_path: Path) -> None:
    context = _context(tmp_path)
    calls = _install_response(monkeypatch, _response(b"ID3fresh", "audio/mpeg"))

    first = await voice.text_to_speech("修复缓存", context)
    assert first is not None
    Path(first).write_bytes(b"<html>")
    second = await voice.text_to_speech("修复缓存", context)

    assert second == first
    assert Path(second).read_bytes() == b"ID3fresh"
    assert len(calls) == 2


def test_cached_audio_handles_read_and_cleanup_failures(caplog) -> None:
    cache = MagicMock()
    unreadable = MagicMock()
    unreadable.stat.side_effect = OSError("unreadable")
    cache.get_any.return_value = unreadable
    assert voice._get_cached_audio(cache, "entry.mp3") is None

    corrupt = MagicMock()
    corrupt.stat.return_value.st_size = 3
    corrupt.open.return_value.__enter__.return_value.readinto.return_value = 0
    corrupt.unlink.side_effect = OSError("locked")
    cache.get_any.return_value = corrupt
    assert voice._get_cached_audio(cache, "entry.mp3") is None
    assert "无法移除" in caplog.text


@pytest.mark.asyncio
async def test_tts_uses_cache_filled_while_waiting_for_same_key(
    monkeypatch, tmp_path: Path
) -> None:
    cached = tmp_path / "filled.mp3"
    cached.write_bytes(b"ID3filled")
    outcomes = iter((None, cached))
    monkeypatch.setattr(voice, "_get_cached_audio", lambda *_args: next(outcomes))
    request = AsyncMock()
    monkeypatch.setattr(voice, "aiohttp_request_bounded", request)

    assert await voice.text_to_speech("singleflight", _context(tmp_path)) == str(cached.resolve())
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_handles_cache_store_rejection(monkeypatch, tmp_path: Path) -> None:
    _install_response(monkeypatch, _response(b"ID3audio", "audio/mpeg"))
    monkeypatch.setattr(
        voice.BoundedFileCache,
        "put_if_absent",
        lambda *_args: (None, False),
    )
    assert await voice.text_to_speech("cache rejection", _context(tmp_path)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["", "   ", None, "x" * (voice.MAX_TTS_TEXT_LENGTH + 1)],
    ids=["empty", "whitespace", "non-string", "too-long"],
)
async def test_tts_rejects_invalid_text_before_http(
    monkeypatch,
    tmp_path: Path,
    text: object,
) -> None:
    request = AsyncMock()
    monkeypatch.setattr(voice, "aiohttp_request_bounded", request)

    assert await voice.text_to_speech(text, _context(tmp_path)) is None
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_rejects_missing_key_and_invalid_audio(monkeypatch, tmp_path: Path) -> None:
    calls = _install_response(monkeypatch, _response(b"not-an-mp3", "audio/mpeg"))

    assert await voice.text_to_speech("hello", _context(tmp_path, {})) is None
    assert await voice.text_to_speech("hello", _context(tmp_path)) is None
    assert len(calls) == 1
    assert list((tmp_path / "voice-data" / "audio").glob("*.mp3")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [HttpStatusError(401), RuntimeError("network failed")])
async def test_tts_handles_transport_failures(
    monkeypatch, tmp_path: Path, exception: Exception
) -> None:
    async def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(voice, "aiohttp_request_bounded", fail)
    assert await voice.text_to_speech("hello", _context(tmp_path)) is None


@pytest.mark.asyncio
async def test_handle_covers_help_empty_unknown_success_and_failure(
    monkeypatch, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    event = {"user_id": 1}
    audio = tmp_path / "answer.mp3"
    audio.write_bytes(b"ID3audio")

    assert "文字转语音" in str(await voice.handle("tts", "帮助", event, context))
    assert "请输入" in str(await voice.handle("tts", "", event, context))
    assert "未知" in str(await voice.handle("other", "x", event, context))

    synthesize = AsyncMock(return_value=str(audio))
    monkeypatch.setattr(voice, "text_to_speech", synthesize)
    invalid_help = await voice.handle("tts", "help extra", event, context)
    assert "不接受额外参数" in str(invalid_help)
    synthesize.assert_not_awaited()

    result = await voice.handle("tts", " 你好 ", event, context)
    assert result == [{"type": "record", "data": {"file": audio.resolve().as_uri()}}]
    synthesize.assert_awaited_once_with("你好", context)

    synthesize.return_value = None
    assert "语音合成失败" in str(await voice.handle("tts", "失败", event, context))


@pytest.mark.asyncio
async def test_handle_reports_overlong_text_before_synthesis(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """用户输入超限属于参数错误，不应伪装成远程语音合成失败。"""
    synthesize = AsyncMock()
    monkeypatch.setattr(voice, "text_to_speech", synthesize)

    result = await voice.handle(
        "tts",
        "x" * (voice.MAX_TTS_TEXT_LENGTH + 1),
        {"user_id": 1},
        _context(tmp_path),
    )

    assert "文字过长" in str(result)
    assert str(voice.MAX_TTS_TEXT_LENGTH) in str(result)
    assert "语音合成失败" not in str(result)
    synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_returns_public_error_on_unexpected_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        voice,
        "text_to_speech",
        AsyncMock(side_effect=RuntimeError("private detail")),
    )
    result = await voice.handle("tts", "hello", {}, _context(tmp_path))
    assert "XQ-PLUGIN-UNEXPECTED" in result[0]["data"]["text"]
    assert "private detail" not in result[0]["data"]["text"]


@pytest.mark.asyncio
async def test_service_callback_builds_record_or_returns_none(monkeypatch, tmp_path: Path) -> None:
    context = _context(tmp_path)
    audio = tmp_path / "service.mp3"
    audio.write_bytes(b"ID3audio")
    synthesize = AsyncMock(return_value=str(audio))
    monkeypatch.setattr(voice, "text_to_speech", synthesize)

    assert await voice.convert_text_to_voice("hello", context) == [
        {"type": "record", "data": {"file": audio.resolve().as_uri()}}
    ]
    synthesize.return_value = None
    assert await voice.convert_text_to_voice("hello", context) is None
