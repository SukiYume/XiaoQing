"""
AI 对话插件 (Coze API)
提供与 AI 的对话功能
"""

import asyncio
import hashlib
import logging
from typing import Any

from core.args import parse
from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.plugin_base import segments
from core.public_errors import public_error_message, public_error_response

logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

COZE_API_URL = "https://api.coze.com/open_api/v2/chat"
DEFAULT_USER_ID = "123223"
REQUEST_TIMEOUT = 30  # 秒
MAX_QUERY_LENGTH = 2000  # 最大查询长度
_API_SEMAPHORE = asyncio.Semaphore(2)
_COZE_BODY_LIMITS = BodyLimits(
    max_wire_bytes=2 * 1024 * 1024,
    max_decoded_bytes=4 * 1024 * 1024,
)
_COZE_JSON_LIMITS = JsonLimits(max_bytes=_COZE_BODY_LIMITS.max_decoded_bytes)
_COZE_JSON_MIME = MimePolicy(
    exact=frozenset({"application/json"}),
    structured_suffixes=frozenset({"+json"}),
)

# ============================================================
# 插件初始化
# ============================================================


def init(context=None) -> None:
    """插件初始化"""
    pass


# ============================================================
# 配置管理
# ============================================================


def get_config(context) -> dict[str, Any]:
    """获取并验证插件配置

    Args:
        context: 插件上下文

    Returns:
        配置字典
    """
    config = context.secrets.get("plugins", {}).get("chat", {})

    # 验证必需的配置项
    if not config:
        context.logger.warning("Chat 插件配置不存在")
    elif not config.get("token"):
        context.logger.warning("Chat 插件缺少 token 配置")
    elif not config.get("bot_id"):
        context.logger.warning("Chat 插件缺少 bot_id 配置")

    return config


def validate_config(config: dict[str, Any]) -> tuple[bool, str | None]:
    """验证配置的完整性

    Args:
        config: 配置字典

    Returns:
        (是否有效, 错误信息)
    """
    if not config:
        return False, "插件配置为空，请在 secrets.json 中配置 chat 插件"

    if not config.get("token"):
        return False, "缺少 token 配置"

    if not config.get("bot_id"):
        return False, "缺少 bot_id 配置"

    return True, None


# ============================================================
# API 调用
# ============================================================


async def call_coze_api(
    query: str,
    config: dict[str, Any],
    context,
    actor_id: Any = None,
) -> dict[str, Any] | None:
    """调用 Coze API

    Args:
        query: 用户查询内容
        config: 插件配置
        context: 插件上下文

    Returns:
        API 响应数据，失败时返回 None
    """
    token = config.get("token")
    bot_id = config.get("bot_id")
    identity_source = f"{bot_id}:{actor_id if actor_id is not None else DEFAULT_USER_ID}"
    user_id = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:32]
    proxy = config.get("proxy")
    if config.get("stream"):
        context.logger.info("Chat 插件已固定使用非流式请求，忽略 stream 配置")

    # 构建请求
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Host": "api.coze.com",
        "Connection": "keep-alive",
    }

    payload = {
        "bot_id": bot_id,
        "user": user_id,
        "query": query,
        "stream": False,
    }

    request_kwargs: dict[str, Any] = {
        "json": payload,
        "timeout": REQUEST_TIMEOUT,
    }

    if proxy:
        request_kwargs["proxy"] = proxy
        context.logger.debug("使用已配置代理")

    # 发送请求
    try:
        context.logger.info(f"调用 Coze API，查询长度: {len(query)} 字符")
        async with _API_SEMAPHORE:
            try:
                response = await aiohttp_request_bounded(
                    context.http_session,
                    "POST",
                    COZE_API_URL,
                    limits=_COZE_BODY_LIMITS,
                    mime_policy=_COZE_JSON_MIME,
                    headers=headers,
                    request_kwargs=request_kwargs,
                )
            except HttpStatusError as exc:
                context.logger.error("Coze API 返回错误: HTTP %s", exc.status)
                return None
            data = parse_bounded_json(response, limits=_COZE_JSON_LIMITS)
            if not isinstance(data, dict):
                raise ValueError("Coze API response must be a JSON object")

            context.logger.info(f"Coze API 调用成功，响应消息数: {len(data.get('messages', []))}")
            return data

    except asyncio.TimeoutError:
        context.logger.error(f"Coze API 请求超时 ({REQUEST_TIMEOUT}s)")
        return None
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="chat.coze_api",
        )
        return None


def extract_answer(data: dict[str, Any], context) -> str | None:
    """从 API 响应中提取答案

    Args:
        data: API 响应数据
        context: 插件上下文

    Returns:
        答案文本，失败时返回 None
    """
    if not isinstance(data, dict):
        context.logger.error(f"API 响应格式错误: 期望字典，得到 {type(data)}")
        return None

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        context.logger.error(f"messages 字段格式错误: 期望列表，得到 {type(messages)}")
        return None

    # 提取类型为 "answer" 的消息
    answers = [msg for msg in messages if isinstance(msg, dict) and msg.get("type") == "answer"]

    if not answers:
        context.logger.warning(f"未找到答案消息，消息总数: {len(messages)}")
        # 尝试查看是否有其他类型的消息
        message_types = [msg.get("type") for msg in messages if isinstance(msg, dict)]
        context.logger.debug(f"消息类型: {message_types}")
        return None

    answer_content = answers[0].get("content", "").strip()
    if not answer_content:
        context.logger.warning("答案内容为空")
        return None

    context.logger.debug(f"提取到答案，长度: {len(answer_content)} 字符")
    return answer_content


# ============================================================
# 命令处理
# ============================================================


async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        parsed = parse(args)

        # 解析子命令
        if parsed and parsed.first:
            subcommand = parsed.first.lower()

            if subcommand == "help" or subcommand == "帮助":
                return segments(_show_help())

        # 解析参数
        query = args.strip()
        if not query:
            return segments("💬 请输入要对话的内容\n\n用法: /chat <你的问题>")

        # 限制查询长度
        if len(query) > MAX_QUERY_LENGTH:
            return segments(
                f"❌ 查询内容过长（{len(query)} 字符），最多支持 {MAX_QUERY_LENGTH} 字符"
            )

        # 获取并验证配置
        config = get_config(context)
        is_valid, error_msg = validate_config(config)
        if not is_valid:
            logger.error(f"Chat 插件配置无效: {error_msg}")
            return segments(f"❌ 配置错误: {error_msg}")

        logger.info("Chat query accepted: user=%s length=%d", event.get("user_id"), len(query))

        state = getattr(context, "state", None)
        if isinstance(state, dict):
            usage = state.setdefault("chat_usage", {})
            actor = str(event.get("user_id"))
            per_user_limit = int(config.get("daily_user_limit", 20))
            global_limit = int(config.get("daily_global_limit", 100))
            if usage.get(actor, 0) >= per_user_limit or usage.get("__total__", 0) >= global_limit:
                return segments("❌ 今日 AI 对话额度已用完")
            usage[actor] = usage.get(actor, 0) + 1
            usage["__total__"] = usage.get("__total__", 0) + 1

        # 调用 API
        data = await call_coze_api(query, config, context, event.get("user_id"))
        if data is None:
            return segments("❌ AI 对话失败，请稍后重试")

        # 提取答案
        answer = extract_answer(data, context)
        if answer is None:
            return segments("❌ 未能获取到有效回答")

        # 返回答案
        return segments(answer)

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="chat.handle",
        )


async def reply(
    text: str,
    event: dict[str, Any],
    context,
) -> list[dict[str, Any]]:
    """Declared smalltalk provider; command and callback names are not exposed."""

    return await handle("chat", text, event, context)


def _show_help() -> str:
    """显示帮助信息"""
    return """
💬 **AI 对话助手**

与 AI 进行智能对话

**使用方法:**
• /chat <问题> - 向 AI 提问
• /gpt <问题> - 向 AI 提问（别名）
• /chat help - 显示帮助信息

**示例:**
• /chat 什么是快速射电暴？
• /gpt 解释一下黑洞的形成过程

**功能特点:**
- 基于先进的 AI 模型
- 支持自然语言对话
- 快速响应

输入 /chat <你的问题> 开始对话
""".strip()
