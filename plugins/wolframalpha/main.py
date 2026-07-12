"""
Wolfram|Alpha 插件

调用 Wolfram|Alpha API 进行计算和查询。
"""

import asyncio
import logging
from xml.etree import ElementTree

import aiohttp

from core.args import parse
from core.bounded_http import (
    JSON_MIME_POLICY,
    XML_MIME_POLICY,
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    ResponseFormatError,
    XmlLimits,
    aiohttp_request_bounded,
    parse_bounded_json,
    validate_bounded_xml,
)
from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response

logger = logging.getLogger(__name__)

WA_RESULT_URL = "https://api.wolframalpha.com/v1/result"
WA_QUERY_URL = "https://api.wolframalpha.com/v2/query"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_LENGTH = 500
MAX_RESULT_ITEMS = 20
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


def _decode_text_response(response) -> str:
    charset = (response.charset or "utf-8").casefold().replace("_", "-")
    if charset not in {"utf-8", "utf8", "us-ascii", "ascii"}:
        raise ResponseFormatError("unsupported WolframAlpha response charset")
    try:
        return response.body.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise ResponseFormatError("invalid WolframAlpha text response") from exc


def init(context=None) -> None:
    """插件初始化"""
    pass


async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)

        # 检查帮助命令
        if (
            not parsed
            or parsed.has("h")
            or parsed.has("help")
            or parsed.first.lower() in ["help", "帮助"]
        ):
            return segments(_show_help())

        # 获取问题内容
        question = parsed.rest()
        if not question:
            return segments("请输入问题\n输入 /alpha help 查看帮助")
        if len(question) > MAX_QUERY_LENGTH:
            return segments(f"❌ 查询过长，最多 {MAX_QUERY_LENGTH} 字符")

        # 获取 App ID
        appid = _get_appid(context)
        if not appid:
            return segments(
                "❌ Wolfram|Alpha 未配置 appid\n请在 secrets.json 中配置 plugins.wolframalpha.appid"
            )

        # 执行查询
        return await _get_answer(question, appid, context)

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="wolframalpha.handle",
        )


def _show_help() -> str:
    """显示帮助信息"""
    return """
🧮 **Wolfram|Alpha 万能计算器**

**基本用法:**
• /alpha <问题> - 查询或计算
• /alpha help - 显示此帮助

**特殊后缀:**
• step - 显示步骤解答
  示例: /alpha integrate x^2 step
  
• cp - 仅返回完整结果
  示例: /alpha 1+1 cp

**查询示例:**
• /alpha 1+1 - 简单计算
• /alpha sin(pi/4) - 三角函数
• /alpha integrate x^2 - 积分
• /alpha solve x^2+2x+1=0 - 方程求解
• /alpha derivative of sin(x) - 求导
• /alpha population of China - 查询数据
• /alpha weather in Beijing - 天气查询
• /alpha convert 100 USD to CNY - 单位转换

**支持的内容:**
• 数学计算（代数、微积分、统计）
• 物理公式和常数
• 化学数据
• 单位转换
• 日期和时间计算
• 地理和天文数据
• 语言翻译

输入 /alpha help 查看此帮助
""".strip()


# ============================================================
# 配置获取
# ============================================================


def _get_appid(context) -> str:
    """获取 App ID"""
    return context.secrets.get("plugins", {}).get("wolframalpha", {}).get("appid", "")


# ============================================================
# 查询处理函数
# ============================================================


async def _get_answer(question: str, appid: str, context) -> list:
    """执行 Wolfram|Alpha 查询"""
    session = context.http_session
    if not session:
        return segments("❌ HTTP 会话未初始化")

    try:
        # 检查是否需要步骤解答
        if question.strip().endswith("step"):
            result = await _query_step(question[:-4].strip(), appid, session)
            return segments(f"📝 **步骤解答:**\n\n{result}")

        # 检查是否需要完整结果
        if question.strip().endswith("cp"):
            result = await _query_complete(question[:-2].strip(), appid, session)
            return segments(f"🔢 **计算结果:**\n\n{result}")

        # 简单查询 - 使用 v1/result API (最快速)
        data = {"appid": appid, "i": question}

        async with _WA_SEMAPHORE:
            try:
                response = await aiohttp_request_bounded(
                    session,
                    "POST",
                    WA_RESULT_URL,
                    limits=_RESPONSE_LIMITS,
                    mime_policy=_TEXT_MIME_POLICY,
                    request_kwargs={"data": data, "timeout": 30},
                )
            except HttpStatusError as exc:
                logger.error("WolframAlpha API error: status=%d", exc.status)
                return segments(f"❌ 查询失败（HTTP {exc.status}）")
            result = _decode_text_response(response)

        return segments(f"🔢 **{question}**\n\n{result}")

    except asyncio.TimeoutError:
        logger.error("WolframAlpha query timeout")
        return segments("❌ 查询超时，请稍后重试")
    except aiohttp.ClientError as exc:
        public_error_message(
            context,
            exc,
            logger=logger,
            component="wolframalpha.network",
        )
        return segments("❌ 网络错误，请稍后重试")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="wolframalpha.query",
        )


async def _query_step(question: str, appid: str, session) -> str:
    """获取步骤解答"""
    data = {
        "appid": appid,
        "input": question,
        "podstate": "Result__Step-by-step solution",
        "format": "plaintext",
    }

    response = await aiohttp_request_bounded(
        session,
        "POST",
        WA_QUERY_URL,
        limits=_RESPONSE_LIMITS,
        mime_policy=XML_MIME_POLICY,
        request_kwargs={"data": data, "timeout": 30},
    )
    payload = validate_bounded_xml(response, limits=_XML_LIMITS)
    root = ElementTree.fromstring(payload)
    lines = []
    for item in list(root.iter("plaintext"))[:MAX_RESULT_ITEMS]:
        if item.text:
            lines.append(item.text.strip())

    if not lines:
        return "未找到步骤解答"

    return "\n\n".join(lines)


async def _query_complete(question: str, appid: str, session) -> str:
    """获取完整结果"""
    data = {
        "appid": appid,
        "input": question,
        "includepodid": "Result",
        "format": "plaintext",
        "output": "json",
    }

    response = await aiohttp_request_bounded(
        session,
        "POST",
        WA_QUERY_URL,
        limits=_RESPONSE_LIMITS,
        mime_policy=JSON_MIME_POLICY,
        request_kwargs={"data": data, "timeout": 30},
    )
    payload = parse_bounded_json(response, limits=_JSON_LIMITS)
    if not isinstance(payload, dict):
        raise ResponseFormatError("WolframAlpha response must be a JSON object")

    # 提取结果
    try:
        pods = payload["queryresult"]["pods"][:MAX_RESULT_ITEMS]
        result = pods[0]["subpods"][0]["plaintext"]
        if not result:
            return "未找到结果"
        return result
    except (KeyError, IndexError) as exc:
        logger.error(
            "Failed to parse WolframAlpha response error_type=%s",
            type(exc).__name__,
        )
        return "结果解析失败"
