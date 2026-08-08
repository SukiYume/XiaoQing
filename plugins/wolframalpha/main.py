"""通过 Wolfram|Alpha 的固定 HTTPS 端点执行有限的计算查询。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, cast
from xml.etree import ElementTree

import aiohttp

from core.args import parse
from core.bounded_http import (
    JSON_MIME_POLICY,
    XML_MIME_POLICY,
    BodyLimits,
    BoundedHttpResponse,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    ResponseFormatError,
    XmlLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
    validate_bounded_xml,
)
from core.interfaces import PluginSettingsSnapshot
from core.plugin_base import has_control_characters as _has_control_chars
from core.plugin_base import segments as _core_segments
from core.public_errors import public_error_message
from core.public_errors import public_error_response as _core_public_error_response

MessageSegment = dict[str, Any]
MessageSegments = list[MessageSegment]
OneBotEvent = dict[str, Any]
QueryMode = Literal["simple", "step", "complete"]


class Context(Protocol):
    """本插件实际读取的最小运行时上下文。"""

    http_session: Any

    def get_settings_snapshot(self) -> PluginSettingsSnapshot: ...


segments = cast(Callable[[object], MessageSegments], _core_segments)
public_error_response = cast(Callable[..., MessageSegments], _core_public_error_response)

logger = logging.getLogger(__name__)

WA_RESULT_URL = "https://api.wolframalpha.com/v1/result"
WA_QUERY_URL = "https://api.wolframalpha.com/v2/query"

MAX_QUERY_LENGTH = 500
MAX_APPID_LENGTH = 128
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESULT_ITEMS = 20
MAX_RESULT_TEXT_LENGTH = 2_400

_APPID_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_APPID_LENGTH}}}\Z")
_HELP_ALIASES = frozenset({"help", "帮助"})
_EXACT_HELP_ARGUMENTS = frozenset({*_HELP_ALIASES, "-h", "--help"})
_SUPPORTED_OPTIONS = frozenset({"h", "help", "mode"})
_MODE_ALIASES: dict[str, QueryMode] = {
    "simple": "simple",
    "step": "step",
    "complete": "complete",
    "cp": "complete",
}
_HELP_TEXT = """🧮 Wolfram|Alpha
用法：/alpha [模式] <问题>

模式
默认 / --mode=simple：快速结果
--mode=step：步骤解答
--mode=complete：完整结果
--mode=cp：complete 的别名

示例
/alpha 1+1
/alpha --mode=step integrate x^2"""

_WA_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=25)
_WA_SEMAPHORE = asyncio.Semaphore(2)
_RESPONSE_LIMITS = BodyLimits(
    max_wire_bytes=MAX_RESPONSE_BYTES,
    max_decoded_bytes=MAX_RESPONSE_BYTES,
    max_decompression_ratio=20,
)
_TEXT_MIME_POLICY = MimePolicy(exact=frozenset({"text/plain"}))
_JSON_LIMITS = JsonLimits(
    max_bytes=MAX_RESPONSE_BYTES,
    max_depth=32,
    max_nodes=20_000,
    max_string_chars=512_000,
)
_XML_LIMITS = XmlLimits(
    max_bytes=MAX_RESPONSE_BYTES,
    max_depth=48,
    max_nodes=20_000,
    max_attributes=20_000,
    max_attribute_chars=256_000,
    max_text_chars=512_000,
)


# ---------------------------------------------------------------------------
# 配置与响应文本收窄
# ---------------------------------------------------------------------------


def _get_appid(context: Context) -> str:
    """从唯一支持的 secret 层级读取并收窄 App ID。"""

    value = context.get_settings_snapshot().plugin_secrets("wolframalpha").get("appid")
    if type(value) is not str or _has_control_chars(value):
        return ""
    appid = value.strip()
    return appid if _APPID_PATTERN.fullmatch(appid) is not None else ""


def _bound_result_text(value: object, *, empty_message: str) -> str:
    """收窄 API 文本，保证最终 OneBot 消息不超过平台预算。"""

    if not isinstance(value, str):
        return empty_message
    text = value.strip()
    if not text:
        return empty_message
    if len(text) <= MAX_RESULT_TEXT_LENGTH:
        return text
    return f"{text[: MAX_RESULT_TEXT_LENGTH - 1].rstrip()}…"


def _decode_text_response(response: BoundedHttpResponse) -> str:
    """仅按受支持的无歧义字符集解码快速查询响应。"""

    charset = (response.charset or "utf-8").casefold().replace("_", "-")
    if charset not in {"utf-8", "utf8", "us-ascii", "ascii"}:
        raise ResponseFormatError("unsupported WolframAlpha response charset")
    try:
        return response.body.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise ResponseFormatError("invalid WolframAlpha text response") from exc


# ---------------------------------------------------------------------------
# Wolfram API 查询与结果解析
# ---------------------------------------------------------------------------


async def _get_answer(
    question: str,
    appid: str,
    context: Context,
    *,
    mode: QueryMode = "simple",
) -> MessageSegments:
    """执行一种查询模式，并把传输错误统一转换为稳定的公开回复。"""

    session = getattr(context, "http_session", None)
    if session is None:
        return segments("❌ HTTP 会话未初始化")

    try:
        if mode == "step":
            result = await _query_step(question, appid, session)
            return segments(f"📝 步骤解答：\n\n{result}")
        if mode == "complete":
            result = await _query_complete(question, appid, session)
            return segments(f"🔢 计算结果：\n\n{result}")

        response = await _request_wolfram(
            session,
            WA_RESULT_URL,
            params={"appid": appid, "i": question},
            mime_policy=_TEXT_MIME_POLICY,
        )
        result = _bound_result_text(
            _decode_text_response(response),
            empty_message="未找到结果",
        )
        return segments(f"🔢 {question}\n\n{result}")
    except HttpStatusError as exc:
        logger.error("WolframAlpha API 返回非成功状态：%d", exc.status)
        return segments(f"❌ 查询失败（HTTP {exc.status}）")
    except asyncio.TimeoutError:
        logger.error("WolframAlpha 查询超时")
        return segments("❌ 查询超时，请稍后重试")
    except aiohttp.ClientError as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="wolframalpha.network",
        )
        return segments("❌ 网络错误，请稍后重试")
    except ResponseFormatError as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="wolframalpha.response",
        )
        return segments("❌ 服务返回格式异常，请稍后重试")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="wolframalpha.query",
        )


async def _query_step(question: str, appid: str, session: Any) -> str:
    """读取步骤模式的有限 XML，并保留前二十个非空文本项。"""

    response = await _request_wolfram(
        session,
        WA_QUERY_URL,
        params={
            "appid": appid,
            "input": question,
            "podstate": "Result__Step-by-step solution",
            "format": "plaintext",
        },
        mime_policy=XML_MIME_POLICY,
    )
    payload = validate_bounded_xml(response, limits=_XML_LIMITS)
    root = ElementTree.fromstring(payload)
    lines: list[str] = []
    for item in root.iter("plaintext"):
        text = item.text.strip() if isinstance(item.text, str) else ""
        if text:
            lines.append(text)
        if len(lines) == MAX_RESULT_ITEMS:
            break
    return _bound_result_text(
        "\n\n".join(lines),
        empty_message="未找到步骤解答",
    )


async def _query_complete(question: str, appid: str, session: Any) -> str:
    """读取完整模式的有限 JSON，安全遍历 Result pod 的纯文本结果。"""

    response = await _request_wolfram(
        session,
        WA_QUERY_URL,
        params={
            "appid": appid,
            "input": question,
            "includepodid": "Result",
            "format": "plaintext",
            "output": "json",
        },
        mime_policy=JSON_MIME_POLICY,
    )
    payload = parse_bounded_json(response, limits=_JSON_LIMITS)
    if not isinstance(payload, Mapping):
        raise ResponseFormatError("WolframAlpha response must be a JSON object")
    query_result = payload.get("queryresult")
    pods = query_result.get("pods") if isinstance(query_result, Mapping) else None
    if not isinstance(pods, list):
        logger.error("WolframAlpha 完整结果缺少 pods 列表")
        return "结果解析失败"

    results: list[str] = []
    for pod in pods[:MAX_RESULT_ITEMS]:
        subpods = pod.get("subpods") if isinstance(pod, Mapping) else None
        if not isinstance(subpods, list):
            continue
        for subpod in subpods:
            plaintext = subpod.get("plaintext") if isinstance(subpod, Mapping) else None
            text = plaintext.strip() if isinstance(plaintext, str) else ""
            if text:
                results.append(text)
            if len(results) == MAX_RESULT_ITEMS:
                break
        if len(results) == MAX_RESULT_ITEMS:
            break
    return _bound_result_text(
        "\n\n".join(results),
        empty_message="未找到结果",
    )


async def _request_wolfram(
    session: Any,
    url: str,
    *,
    params: dict[str, str],
    mime_policy: MimePolicy,
) -> BoundedHttpResponse:
    """按 Wolfram API 的 GET 约定发送有界请求。"""

    async with _WA_SEMAPHORE:
        response = await aiohttp_request_bounded(
            session,
            "GET",
            url,
            limits=_RESPONSE_LIMITS,
            mime_policy=mime_policy,
            request_kwargs={"params": params, "timeout": _WA_TIMEOUT},
        )
    return response


# ---------------------------------------------------------------------------
# 聊天命令入口
# ---------------------------------------------------------------------------


async def handle(
    command: str,
    args: str,
    event: OneBotEvent,
    context: Context,
) -> MessageSegments:
    """解析管理员查询命令并选择显式查询模式。"""

    del event
    try:
        if command != "alpha":
            return segments("未知命令")

        parsed = parse(args)
        if not parsed or args.strip().casefold() in _EXACT_HELP_ARGUMENTS:
            return segments(_HELP_TEXT)
        if parsed.has("h") or parsed.has("help"):
            return segments("❌ 帮助选项不接受额外参数")
        if unsupported := set(parsed.options) - _SUPPORTED_OPTIONS:
            names = "、".join(f"--{name}" for name in sorted(unsupported))
            return segments(f"❌ 不支持的选项：{names}")

        mode = _MODE_ALIASES.get(parsed.opt("mode", "simple").strip().casefold())
        if mode is None:
            return segments("❌ mode 仅支持 simple、step、complete 或 cp")

        # 模式必须通过选项指定；普通问题末尾的 step/cp 始终属于正文。
        question = parsed.rest().strip()
        if not question:
            return segments("请输入问题\n输入 /alpha help 查看帮助")
        if len(question) > MAX_QUERY_LENGTH:
            return segments(f"❌ 查询过长，最多 {MAX_QUERY_LENGTH} 字符")
        if _has_control_chars(question):
            return segments("❌ 查询包含不支持的控制字符")

        appid = _get_appid(context)
        if not appid:
            return segments(
                "❌ Wolfram|Alpha 未配置有效 appid\n"
                "请在 config/secrets.json 的 plugins.wolframalpha.appid 中配置"
            )
        return await _get_answer(question, appid, context, mode=mode)
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="wolframalpha.handle",
        )
