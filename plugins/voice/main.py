"""通过 Azure Speech 提供有界的文字转语音和内部语音识别能力。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

import aiohttp

from core.async_keyed_lock import AsyncKeyedLockPool
from core.bounded_file_cache import BoundedFileCache, FileCacheLimits
from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import has_control_characters as _has_control_chars
from core.plugin_base import record as _core_record
from core.plugin_base import segments as _core_segments
from core.public_errors import public_error_message
from core.public_errors import public_error_response as _core_public_error_response

MessageSegment = dict[str, Any]
MessageSegments = list[MessageSegment]
OneBotEvent = dict[str, Any]


class Context(Protocol):
    """本插件实际读取的最小运行时上下文。"""

    data_dir: Path
    http_session: Any

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


segments = cast(Callable[[object], MessageSegments], _core_segments)
record = cast(Callable[[str], MessageSegment], _core_record)
public_error_response = cast(Callable[..., MessageSegments], _core_public_error_response)

logger = logging.getLogger(__name__)

DEFAULT_REGION = "southeastasia"
DEFAULT_VOICE_NAME = "zh-CN-XiaomoNeural"
DEFAULT_STYLE = "cheerful"
DEFAULT_ROLE = "Girl"

MAX_TTS_TEXT_LENGTH = 500
MAX_STT_RESULT_LENGTH = 3_000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_PROXY_LENGTH = 2_048

_AZURE_API_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
_REGION_PATTERN = re.compile(r"[a-z0-9-]{1,32}\Z")
_VOICE_NAME_PATTERN = re.compile(r"[A-Za-z0-9-]{1,128}\Z")
_VOICE_OPTION_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_HELP_ALIASES = frozenset({"help", "帮助"})
_HELP_TEXT = (
    "🔊 语音功能使用方法：\n\n"
    "文字转语音：/语音 <文本>、/念 <文本> 或 /tts <文本>\n"
    "示例：/语音 你好，我是小青\n\n"
    "当前只公开文字转语音命令；speech_to_text() 仅供插件内部调用。"
)

_VOICE_SEMAPHORE = asyncio.Semaphore(2)
_TTS_LOCKS = AsyncKeyedLockPool(max_keys=4_096)
_TTS_CACHE_LIMITS = FileCacheLimits(
    max_entries=2_048,
    max_bytes=MAX_CACHE_BYTES,
    ttl_seconds=7 * 24 * 60 * 60,
)
_TTS_BODY_LIMITS = BodyLimits(
    max_wire_bytes=MAX_AUDIO_BYTES,
    max_decoded_bytes=MAX_AUDIO_BYTES,
)
_TTS_MIME = MimePolicy(
    exact=frozenset({"application/octet-stream"}),
    type_prefixes=frozenset({"audio/"}),
)
_STT_BODY_LIMITS = BodyLimits(
    max_wire_bytes=1024 * 1024,
    max_decoded_bytes=2 * 1024 * 1024,
)
_STT_JSON_LIMITS = JsonLimits(max_bytes=_STT_BODY_LIMITS.max_decoded_bytes)
_STT_JSON_MIME = MimePolicy(
    exact=frozenset({"application/json"}),
    structured_suffixes=frozenset({"+json"}),
)


@dataclass(frozen=True, slots=True)
class _VoiceSettings:
    """经过收窄的 Azure Speech 配置，避免原始 secret 值进入 URL 或 SSML。"""

    subscription_key: str
    region: str
    voice_name: str
    style: str
    role: str
    proxy: str | None


def init(context: Context | None = None) -> None:
    """记录插件加载完成。"""

    del context
    logger.info("语音功能插件已加载")


def _get_config(context: Context) -> Mapping[str, object]:
    """读取当前原子设置代中的插件密钥配置。"""

    return context.get_settings_snapshot().plugin_secrets("voice")


def _clean_config_text(
    value: object,
    *,
    default: str,
    max_chars: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    """收窄短字符串配置；非法显式值回退到安全默认值。"""

    if type(value) is not str:
        return default
    if _has_control_chars(value):
        return default
    text = value.strip()
    if (
        not text
        or len(text) > max_chars
        or (pattern is not None and pattern.fullmatch(text) is None)
    ):
        return default
    return text


def _get_proxy(config: Mapping[str, object]) -> str | None:
    """只接受结构完整的 HTTP(S) 代理地址。"""

    value = config.get("proxy")
    if type(value) is not str:
        return None
    if _has_control_chars(value):
        return None
    proxy = value.strip()
    if not proxy or len(proxy) > MAX_PROXY_LENGTH:
        return None
    try:
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return None
    return proxy


def _get_settings(context: Context) -> _VoiceSettings | None:
    """构造 TTS/STT 共用配置；缺少订阅密钥时明确返回 ``None``。"""

    config = _get_config(context)
    subscription_key = _clean_config_text(
        config.get("subscription_key"),
        default="",
        max_chars=512,
    )
    if not subscription_key:
        return None
    return _VoiceSettings(
        subscription_key=subscription_key,
        region=_clean_config_text(
            config.get("region"),
            default=DEFAULT_REGION,
            max_chars=32,
            pattern=_REGION_PATTERN,
        ),
        voice_name=_clean_config_text(
            config.get("voice_name"),
            default=DEFAULT_VOICE_NAME,
            max_chars=128,
            pattern=_VOICE_NAME_PATTERN,
        ),
        style=_clean_config_text(
            config.get("style"),
            default=DEFAULT_STYLE,
            max_chars=64,
            pattern=_VOICE_OPTION_PATTERN,
        ),
        role=_clean_config_text(
            config.get("role"),
            default=DEFAULT_ROLE,
            max_chars=64,
            pattern=_VOICE_OPTION_PATTERN,
        ),
        proxy=_get_proxy(config),
    )


def _looks_like_mp3(payload: bytes) -> bool:
    """检查常见 ID3 或 MPEG 音频帧头，拒绝 HTML/JSON 等伪音频响应。"""

    return payload.startswith(b"ID3") or (
        len(payload) >= 2 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0
    )


def _get_cached_audio(cache: BoundedFileCache, filename: str) -> Path | None:
    """读取并校验缓存头；损坏的旧缓存会被移除，随后允许重新生成。"""

    path = cast(Path | None, cache.get_any((filename,)))
    if path is None:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header_buffer = bytearray(3)
            header_size = stream.readinto(header_buffer)
            header = bytes(header_buffer[:header_size])
    except OSError:
        return None
    if 3 <= size <= MAX_AUDIO_BYTES and _looks_like_mp3(header):
        return path
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("无法移除损坏的 TTS 缓存文件")
    return None


def _read_valid_wav(path: Path) -> bytes | None:
    """一次性有界读取并验证 Azure STT 声明所要求的 PCM WAV。"""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            payload_buffer = bytearray(MAX_AUDIO_BYTES + 1)
            payload_size = stream.readinto(payload_buffer)
            payload = bytes(payload_buffer[:payload_size])
    except OSError:
        return None
    if not payload or len(payload) > MAX_AUDIO_BYTES:
        return None

    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            frame_count = wav.getnframes()
            frame_rate = wav.getframerate()
            valid_format = (
                wav.getcomptype() == "NONE"
                and wav.getnchannels() == 1
                and wav.getsampwidth() == 2
                and frame_rate == 16_000
            )
            if not valid_format or frame_count <= 0 or frame_count / frame_rate > MAX_AUDIO_SECONDS:
                return None
            # wave 头声明的帧数必须与实际 PCM 数据一致，避免上传截断文件。
            if len(wav.readframes(frame_count)) != frame_count * 2:
                return None
    except (EOFError, OSError, wave.Error):
        return None
    return payload


def _tts_cache_key(text: str, settings: _VoiceSettings) -> str:
    """把正文与全部音色设置纳入内容寻址键，防止配置切换后误命中。"""

    material = "\0".join(
        (text, settings.region, settings.voice_name, settings.style, settings.role)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def text_to_speech(text: str, context: Context) -> str | None:
    """把有限文本转换为本地 MP3；失败时返回 ``None``。"""

    normalized_text = text.strip() if type(text) is str else ""
    if not normalized_text or len(normalized_text) > MAX_TTS_TEXT_LENGTH:
        logger.warning("Azure TTS 文本为空或超过长度上限")
        return None

    settings = _get_settings(context)
    if settings is None:
        logger.warning("Azure TTS 未配置 subscription_key")
        return None

    try:
        cache_key = _tts_cache_key(normalized_text, settings)
        filename = f"tts_{cache_key}.mp3"
        cache = BoundedFileCache(Path(context.data_dir) / "audio", _TTS_CACHE_LIMITS)
        output_file = await asyncio.to_thread(_get_cached_audio, cache, filename)
        if output_file is not None:
            return str(output_file.resolve())

        # 所有属性和用户正文都经过 XML 转义，配置不能改变 SSML 结构。
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
            f"<voice name={quoteattr(settings.voice_name)}>"
            f"<mstts:express-as role={quoteattr(settings.role)} "
            f"style={quoteattr(settings.style)}>"
            f"{xml_escape(normalized_text)}"
            "</mstts:express-as></voice></speak>"
        )
        url = f"https://{settings.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": settings.subscription_key,
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "Content-Type": "application/ssml+xml",
            "User-Agent": "XiaoQing/1.0",
        }

        async with _TTS_LOCKS.hold(cache_key):
            # 等待同键请求期间，先发请求的协程可能已经填好缓存。
            output_file = await asyncio.to_thread(_get_cached_audio, cache, filename)
            if output_file is not None:
                return str(output_file.resolve())

            request_kwargs: dict[str, Any] = {
                "data": ssml.encode("utf-8"),
                "timeout": _AZURE_API_TIMEOUT,
            }
            if settings.proxy is not None:
                request_kwargs["proxy"] = settings.proxy
            async with _VOICE_SEMAPHORE:
                try:
                    response = await aiohttp_request_bounded(
                        context.http_session,
                        "POST",
                        url,
                        limits=_TTS_BODY_LIMITS,
                        mime_policy=_TTS_MIME,
                        headers=headers,
                        request_kwargs=request_kwargs,
                        accept_encoding="identity",
                    )
                except HttpStatusError as exc:
                    logger.error("Azure TTS API 返回非成功状态：%s", exc.status)
                    return None

            content = response.body
            if not _looks_like_mp3(content):
                logger.error("Azure TTS 返回了无效的 MP3 内容")
                return None
            output_file, _created = await asyncio.to_thread(
                cache.put_if_absent,
                filename,
                content,
            )
            if output_file is None:
                logger.error("Azure TTS 音频超过缓存预算")
                return None
            logger.info("Azure TTS 合成完成：bytes=%d", len(content))
            return str(output_file.resolve())
    except Exception as exc:
        public_error_message(context, exc, logger=logger, component="voice.tts")
        return None


async def speech_to_text(audio_path: str, context: Context) -> tuple[str, str] | None:
    """识别有限的 16 kHz、单声道、16 位 PCM WAV，供其他插件内部调用。"""

    settings = _get_settings(context)
    if settings is None:
        logger.warning("Azure STT 未配置 subscription_key")
        return None

    try:
        path = Path(audio_path)
        audio_data = await asyncio.to_thread(_read_valid_wav, path)
        if audio_data is None:
            logger.warning("Azure STT 拒绝了无效或超限的 PCM WAV")
            return None

        url = (
            f"https://{settings.region}.stt.speech.microsoft.com/"
            "speech/recognition/conversation/cognitiveservices/v1"
            "?language=zh-CN&format=detailed"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": settings.subscription_key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
            "Accept": "application/json",
        }
        request_kwargs: dict[str, Any] = {
            "data": audio_data,
            "timeout": _AZURE_API_TIMEOUT,
        }
        if settings.proxy is not None:
            request_kwargs["proxy"] = settings.proxy

        async with _VOICE_SEMAPHORE:
            try:
                response = await aiohttp_request_bounded(
                    context.http_session,
                    "POST",
                    url,
                    limits=_STT_BODY_LIMITS,
                    mime_policy=_STT_JSON_MIME,
                    headers=headers,
                    request_kwargs=request_kwargs,
                )
            except HttpStatusError as exc:
                logger.error("Azure STT API 返回非成功状态：%s", exc.status)
                return None

        data = parse_bounded_json(response, limits=_STT_JSON_LIMITS)
        if not isinstance(data, dict):
            raise ValueError("Azure STT 响应不是 JSON 对象")
        candidates = data.get("NBest")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], dict)
        ):
            logger.warning("Azure STT 未识别到语音内容")
            return None

        lexical = candidates[0].get("Lexical")
        display = candidates[0].get("Display")
        if not isinstance(lexical, str) or not isinstance(display, str):
            logger.warning("Azure STT 返回了无效的识别字段")
            return None
        lexical = lexical.strip()
        display = display.strip()
        if (
            not lexical
            or not display
            or len(lexical) > MAX_STT_RESULT_LENGTH
            or len(display) > MAX_STT_RESULT_LENGTH
        ):
            logger.warning("Azure STT 识别文本为空或超过长度上限")
            return None
        logger.info(
            "Azure STT 识别完成：lexical_length=%d display_length=%d",
            len(lexical),
            len(display),
        )
        return lexical, display
    except Exception as exc:
        public_error_message(context, exc, logger=logger, component="voice.stt")
        return None


async def handle(
    command: str,
    args: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """处理管理员 TTS 命令；命令别名已由插件清单统一解析。"""

    del event
    try:
        if command != "tts":
            return segments("未知命令")
        text = args.strip()
        text_parts = text.split(maxsplit=1)
        if text_parts and text_parts[0].casefold() in _HELP_ALIASES:
            if len(text_parts) == 1:
                return segments(_HELP_TEXT)
            return segments("❌ help 子命令不接受额外参数\n用法: /语音 help")
        if not text:
            return segments("请输入要转换的文字，例如：/语音 你好")
        if len(text) > MAX_TTS_TEXT_LENGTH:
            return segments(f"文字过长，请控制在 {MAX_TTS_TEXT_LENGTH} 个字符以内")
        audio_path = await text_to_speech(text, context)
        if audio_path is None:
            return segments("语音合成失败")
        return [record(audio_path)]
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="voice.handle")


async def convert_text_to_voice(text: str, context: Context) -> MessageSegments | None:
    """把文本转换为语音消息段，作为 ``voice.synthesize_text`` 服务回调。"""

    audio_path = await text_to_speech(text, context)
    return [record(audio_path)] if audio_path is not None else None
