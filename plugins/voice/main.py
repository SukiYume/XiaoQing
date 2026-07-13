"""
语音功能插件

实现功能：
1. 文字转语音(TTS) - 使用 Azure 认知服务
2. 提供语音识别(STT)工具函数供其他插件内部调用
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import wave
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import aiohttp

from core.args import parse
from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.plugin_base import PluginContextProtocol, atomic_write_bytes, segments
from core.public_errors import public_error_message, public_error_response

logger = logging.getLogger(__name__)

_AZURE_API_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
MAX_TTS_TEXT_LENGTH = 500
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
MAX_CACHE_BYTES = 256 * 1024 * 1024
_VOICE_SEMAPHORE = asyncio.Semaphore(2)
_TTS_LOCKS: dict[str, asyncio.Lock] = {}
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


def _cleanup_audio_cache(audio_dir: Path) -> None:
    files = sorted(
        (path for path in audio_dir.glob("tts_*.mp3") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    cutoff = time.time() - 7 * 86400
    for path in files:
        size = path.stat().st_size
        total += size
        if path.stat().st_mtime < cutoff or total > MAX_CACHE_BYTES:
            path.unlink(missing_ok=True)


def init(context=None) -> None:
    """插件初始化"""
    logger.info("语音功能插件已加载 (Voice Plugin Loaded)")


def _show_help() -> str:
    """返回帮助信息"""
    return (
        "🔊 语音功能使用方法：\n\n"
        "1. 文字转语音：\n"
        "   /语音 <文本> 或 /念 <文本> 或 /tts <文本>\n"
        "   例：/语音 你好，我是小青\n\n"
        "2. 查看帮助：\n"
        "   /语音 help\n\n"
        "💡 当前命令面开放文字转语音；speech_to_text() 供其他插件内部调用"
    )


# ============================================================
# TTS (文字转语音)
# ============================================================


async def text_to_speech(text: str, context: PluginContextProtocol) -> str | None:
    """
    将文字转换为语音文件

    参数:
        text: 要转换的文字
        context: 插件上下文

    返回:
        生成的音频文件路径，失败返回 None
    """
    # 获取配置
    voice_config = context.secrets.get("plugins", {}).get("voice", {})
    subscription_key = voice_config.get("subscription_key")
    region = voice_config.get("region", "southeastasia")
    voice_name = voice_config.get("voice_name", "zh-CN-XiaomoNeural")
    style = voice_config.get("style", "cheerful")
    role = voice_config.get("role", "Girl")
    proxy = voice_config.get("proxy")

    if not subscription_key:
        logger.warning("Azure TTS 未配置 subscription_key")
        return None
    if not text or len(text) > MAX_TTS_TEXT_LENGTH:
        logger.warning("Azure TTS text rejected: length=%d", len(text))
        return None

    # 生成唯一文件名（文本 + 音色配置），避免不同音色误命中同一缓存
    cache_material = "|".join([text, region, voice_name, style, role])
    text_hash = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
    audio_dir = context.data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_cleanup_audio_cache, audio_dir)
    output_file = audio_dir / f"tts_{text_hash}.mp3"

    # 如果文件已存在，直接返回
    if output_file.exists():
        logger.info(f"使用缓存音频: {output_file}")
        return str(output_file.absolute())

    # 构建 SSML（对用户输入进行 XML 转义，防止 SSML 注入）
    safe_text = xml_escape(text)
    ssml = f'''
    <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis"
           xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">
        <voice name="{voice_name}">
            <mstts:express-as role="{role}" style="{style}">
                {safe_text}
            </mstts:express-as>
        </voice>
    </speak>
    '''.strip()

    # 调用 Azure TTS API
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key,
        "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
        "Content-Type": "application/ssml+xml",
        "User-Agent": "XiaoQing/1.0",
    }

    try:
        lock = _TTS_LOCKS.setdefault(text_hash, asyncio.Lock())
        async with lock:
            if output_file.exists():
                return str(output_file.absolute())
            async with _VOICE_SEMAPHORE:
                request_kwargs: dict[str, Any] = {
                    "data": ssml.encode("utf-8"),
                    "timeout": _AZURE_API_TIMEOUT,
                }
                if proxy:
                    request_kwargs["proxy"] = proxy
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
                    logger.error("Azure TTS API 错误 %s", exc.status)
                    return None
                content = response.body
                await asyncio.to_thread(atomic_write_bytes, output_file, content)
                logger.info("Azure TTS generated audio: bytes=%d", len(content))
                return str(output_file.absolute())
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="voice.tts",
        )
        return None


# ============================================================
# STT (语音转文字)
# ============================================================


async def speech_to_text(
    audio_path: str, context: PluginContextProtocol
) -> tuple[str, str] | None:
    """
    将语音文件转换为文字

    参数:
        audio_path: 音频文件路径
        context: 插件上下文

    返回:
        (分词文本, 完整文本) 元组，失败返回 None
    """
    # 获取配置
    voice_config = context.secrets.get("plugins", {}).get("voice", {})
    subscription_key = voice_config.get("subscription_key")
    region = voice_config.get("region", "eastasia")  # STT 使用 eastasia
    proxy = voice_config.get("proxy")

    if not subscription_key:
        logger.warning("Azure STT 未配置 subscription_key")
        return None

    audio_file = Path(audio_path)
    if not audio_file.is_file() or audio_file.stat().st_size > MAX_AUDIO_BYTES:
        logger.warning("Azure STT audio rejected by byte limit")
        return None
    try:
        with wave.open(str(audio_file), "rb") as wav:
            duration = wav.getnframes() / max(1, wav.getframerate())
        if duration > MAX_AUDIO_SECONDS:
            logger.warning("Azure STT audio rejected by duration limit")
            return None
    except (wave.Error, EOFError):
        pass

    # 调用 Azure STT API
    url = f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    url += "?language=zh-CN&format=detailed"

    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key,
        "Content-type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "Accept": "application/json",
    }

    try:
        audio_data = await asyncio.to_thread(audio_file.read_bytes)

        request_kwargs: dict[str, Any] = {
            "data": audio_data,
            "timeout": _AZURE_API_TIMEOUT,
        }
        if proxy:
            request_kwargs["proxy"] = proxy
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
            logger.error("Azure STT API 错误 %s", exc.status)
            return None
        data = parse_bounded_json(response, limits=_STT_JSON_LIMITS)
        if not isinstance(data, dict):
            raise ValueError("Azure STT response must be a JSON object")
        nbest = data.get("NBest")
        if isinstance(nbest, list) and nbest and isinstance(nbest[0], dict):
            lexical = nbest[0].get("Lexical", "")
            display = nbest[0].get("Display", "")
            lexical = lexical if isinstance(lexical, str) else ""
            display = display if isinstance(display, str) else ""
            logger.info(
                "Azure STT succeeded: lexical_length=%d display_length=%d",
                len(lexical),
                len(display),
            )
            return (lexical, display)
        logger.warning("未识别到语音内容")
        return None
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="voice.stt",
        )
        return None


# ============================================================
# 命令处理
# ============================================================


async def handle(command: str, args: str, event: dict, context: PluginContextProtocol) -> list:
    """
    命令处理入口

    参数:
        command: plugin.json 中定义的 command name
        args: 命令后的参数字符串
        event: 原始 OneBot 事件
        context: 插件上下文

    返回:
        消息段列表
    """
    try:
        # 使用 parse 解析参数
        parsed = parse(args)

        if command == "tts":
            # 检查是否请求帮助
            if parsed and parsed.first.lower() in ["help", "帮助"]:
                return segments(_show_help())

            return await _handle_tts(args, context)

        return segments("未知命令")

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="voice.handle",
        )


async def _handle_tts(args: str, context: PluginContextProtocol) -> list[dict[str, Any]]:
    """处理 TTS 命令"""
    text = args.strip()

    if not text:
        return segments("请输入要转换的文字，例如: 语音 你好")

    # 调用 TTS
    audio_path = await text_to_speech(text, context)

    if not audio_path:
        return segments("语音合成失败")

    # 返回语音消息
    return [{"type": "record", "data": {"file": Path(audio_path).resolve().as_uri()}}]


# ============================================================
# 工具函数（供其他插件调用）
# ============================================================


async def convert_text_to_voice(
    text: str, context: PluginContextProtocol
) -> list[dict[str, Any]] | None:
    """
    将文本转换为语音消息段

    供其他插件调用的工具函数

    参数:
        text: 要转换的文字
        context: 插件上下文

    返回:
        语音消息段列表，失败返回 None
    """
    audio_path = await text_to_speech(text, context)

    if not audio_path:
        return None

    return [{"type": "record", "data": {"file": Path(audio_path).resolve().as_uri()}}]
