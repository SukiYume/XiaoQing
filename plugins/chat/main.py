"""
AI 对话插件 (Coze API)
提供与 AI 的对话功能
"""

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.async_keyed_lock import AsyncKeyedLockPool
from core.atomic_store import AtomicJsonStore, keyed_path_lock
from core.bounded_http import (
    BodyLimits,
    HttpStatusError,
    JsonLimits,
    MimePolicy,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.interfaces import PluginContextProtocol
from core.plugin_base import run_sync, segments
from core.public_errors import public_error_message, public_error_response

# ============================================================
# 常量配置
# ============================================================

COZE_API_URL = "https://api.coze.com/v3/chat"
COZE_RETRIEVE_URL = f"{COZE_API_URL}/retrieve"
COZE_MESSAGES_URL = f"{COZE_API_URL}/message/list"
COZE_CANCEL_URL = f"{COZE_API_URL}/cancel"
REQUEST_TIMEOUT = 30  # 秒
POLL_INTERVAL_SECONDS = 1.0
_CANCEL_TIMEOUT_SECONDS = 3.0
MAX_QUERY_LENGTH = 2000  # 最大查询长度
MAX_CONFIG_STRING_LENGTH = 4096
MAX_DAILY_QUOTA = 1_000_000
DEFAULT_DAILY_USER_LIMIT = 20
DEFAULT_DAILY_GLOBAL_LIMIT = 100
_ANONYMOUS_ACTOR = "anonymous"
_CHAT_STATUSES = frozenset(
    {"created", "in_progress", "completed", "failed", "requires_action", "canceled"}
)
_PENDING_CHAT_STATUSES = frozenset({"created", "in_progress"})
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
_QUOTA_LOCKS = AsyncKeyedLockPool(max_keys=1024)
_QUOTA_FILENAME = "chat_quota.json"
_QUOTA_LOCK_KEY = "chat-quota"

HELP_TEXT = """
💬 AI 对话助手

与 Coze 智能体进行单轮对话。

用法
/chat <问题>

别名
/gpt <问题>
/ai <问题>

帮助
/chat help

每位用户和全局调用量均受每日额度限制；远端失败不会消耗额度。
""".strip()


class ChatQuotaExceeded(RuntimeError):
    """无法预留当日对话额度。"""


class ChatQuotaStateError(RuntimeError):
    """本地额度状态损坏，不能安全地把额度重置为零。"""


def _config_string(value: Any, *, allow_empty: bool = False) -> str | None:
    """验证会进入请求头、请求体或代理参数的配置字符串。"""

    if not isinstance(value, str) or value != value.strip():
        return None
    if not value:
        return "" if allow_empty else None
    if len(value) > MAX_CONFIG_STRING_LENGTH or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        return None
    return value


def _quota_limit(config: Mapping[str, Any], key: str, default: int) -> int:
    """读取有界正整数配额，拒绝布尔值和隐式数值转换。"""

    value = config.get(key, default)
    if type(value) is not int or not 1 <= value <= MAX_DAILY_QUOTA:
        raise ValueError(f"{key} 必须是 1 到 {MAX_DAILY_QUOTA} 的整数")
    return value


def _actor_identity(value: Any) -> tuple[str, str | int | None]:
    """生成有界配额键，并只把可信标量参与远端身份摘要。"""

    if type(value) is int and value >= 0:
        return str(value), value
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    ):
        return value, value
    return _ANONYMOUS_ACTOR, None


def _business_date(context: PluginContextProtocol) -> str:
    """从 Core 的配置时钟读取业务日期，避免插件再次解析全局时区。"""

    current = context.now()
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ChatQuotaStateError("configured clock did not return an aware datetime")
    return current.date().isoformat()


@dataclass(slots=True)
class _QuotaReservation:
    path: Path | None
    window: str
    actor: str
    active: bool = True

    def commit(self) -> None:
        self.active = False

    async def rollback(self) -> None:
        if not self.active:
            return
        if self.path is None:
            self.active = False
            return
        async with _QUOTA_LOCKS.hold(_QUOTA_LOCK_KEY):
            if not self.active:
                return
            try:
                await run_sync(_rollback_quota_file, self.path, self.window, self.actor)
            finally:
                # 只有拿到状态锁后才结束预留，避免等待锁时取消导致额度永久泄漏。
                self.active = False


def _quota_path(context: PluginContextProtocol) -> Path:
    data_dir = getattr(context, "data_dir", None)
    if not isinstance(data_dir, (str, Path)):
        raise ChatQuotaStateError("chat quota data directory is unavailable")
    return Path(data_dir) / _QUOTA_FILENAME


def _empty_usage(window: str) -> dict[str, Any]:
    return {"window": window, "users": {}, "total": 0}


def _validated_usage(raw: object, window: str) -> dict[str, Any]:
    if raw is None:
        return _empty_usage(window)
    if not isinstance(raw, Mapping):
        raise ChatQuotaStateError("chat quota state root is invalid")
    if raw.get("window") != window:
        return _empty_usage(window)
    users = raw.get("users")
    total = raw.get("total")
    if (
        not isinstance(users, Mapping)
        or type(total) is not int
        or total < 0
        or any(
            type(key) is not str or type(count) is not int or count < 0
            for key, count in users.items()
        )
        or sum(users.values()) != total
    ):
        raise ChatQuotaStateError("chat quota state contents are invalid")
    return {"window": window, "users": dict(users), "total": total}


def _reserve_quota_file(
    path: Path,
    window: str,
    actor: str,
    per_user_limit: int,
    global_limit: int,
) -> None:
    with keyed_path_lock(path):
        store = AtomicJsonStore(path)
        usage = _validated_usage(store.read(None, raise_on_error=True), window)
        users = usage["users"]
        actor_count = users.get(actor, 0)
        if actor_count >= per_user_limit or usage["total"] >= global_limit:
            raise ChatQuotaExceeded
        users[actor] = actor_count + 1
        usage["total"] += 1
        store.write(usage)


def _rollback_quota_file(path: Path, window: str, actor: str) -> None:
    with keyed_path_lock(path):
        store = AtomicJsonStore(path)
        usage = _validated_usage(store.read(None, raise_on_error=True), window)
        # `_validated_usage` 已把其他日期映射为空的当前窗口；这里只需按计数决定是否回滚。
        users = usage["users"]
        actor_count = users.get(actor, 0)
        if actor_count <= 0 or usage["total"] <= 0:
            return
        if actor_count == 1:
            users.pop(actor, None)
        else:
            users[actor] = actor_count - 1
        usage["total"] -= 1
        store.write(usage)


async def _reserve_quota(
    context: PluginContextProtocol,
    *,
    actor: str,
    per_user_limit: int,
    global_limit: int,
) -> _QuotaReservation:
    window = _business_date(context)
    path = _quota_path(context)
    async with _QUOTA_LOCKS.hold(_QUOTA_LOCK_KEY):
        try:
            await run_sync(
                _reserve_quota_file,
                path,
                window,
                actor,
                per_user_limit,
                global_limit,
            )
        except ChatQuotaExceeded:
            raise
        except (OSError, TypeError, ValueError, ChatQuotaStateError) as exc:
            raise ChatQuotaStateError("chat quota state cannot be read or written") from exc
    return _QuotaReservation(path, window, actor)


def validate_config(config: Mapping[str, Any]) -> tuple[bool, str | None]:
    """验证密钥、Bot ID 和可选代理配置。"""

    if not config:
        return False, "插件配置为空，请在 secrets.json 中配置 chat 插件"

    if _config_string(config.get("token")) is None:
        return False, "token 必须是非空、无控制字符的字符串"

    if _config_string(config.get("bot_id")) is None:
        return False, "bot_id 必须是非空、无控制字符的字符串"

    proxy = config.get("proxy", "")
    if _config_string(proxy, allow_empty=True) is None:
        return False, "proxy 必须是无控制字符的字符串"

    return True, None


# ============================================================
# API 调用
# ============================================================


async def _request_coze_json(
    context: PluginContextProtocol,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    request_kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """发送一次有界 Coze JSON 请求，并统一检查 HTTP 与业务错误。"""

    try:
        response = await aiohttp_request_bounded(
            context.http_session,
            method,
            url,
            limits=_COZE_BODY_LIMITS,
            mime_policy=_COZE_JSON_MIME,
            headers=headers,
            request_kwargs=request_kwargs,
        )
    except HttpStatusError as exc:
        context.logger.error("Coze API 返回 HTTP 错误: status=%s", exc.status)
        return None

    payload = parse_bounded_json(response, limits=_COZE_JSON_LIMITS)
    if not isinstance(payload, dict):
        raise ValueError("Coze API 响应必须是 JSON 对象")

    code = payload.get("code", 0)
    if type(code) is not int or code != 0:
        safe_code = code if type(code) is int else "invalid"
        context.logger.error("Coze API 返回业务错误: code=%s", safe_code)
        return None
    return payload


async def _request_coze_json_before_deadline(
    context: PluginContextProtocol,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    request_kwargs: Mapping[str, Any],
    deadline: float,
) -> dict[str, Any] | None:
    """用整轮剩余预算同时约束 aiohttp 和不遵守参数的替代实现。"""

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(
        _request_coze_json(
            context,
            method,
            url,
            headers=headers,
            request_kwargs={**request_kwargs, "timeout": remaining},
        ),
        timeout=remaining,
    )


def _coze_identifier(value: Any) -> str | None:
    """只接收 Coze 当前使用的有界 ASCII 标识符。"""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or not value.isascii()
        or not all(char.isalnum() or char in "_-" for char in value)
    ):
        return None
    return value


def _chat_state(payload: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """从 v3 对话响应中提取受限的对话 ID、会话 ID 和状态。"""

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None

    chat_id = _coze_identifier(data.get("id"))
    conversation_id = _coze_identifier(data.get("conversation_id"))
    if chat_id is None or conversation_id is None:
        return None

    raw_status = data.get("status")
    if not isinstance(raw_status, str):
        return None
    status = raw_status.casefold()
    if status not in _CHAT_STATUSES:
        return None
    return chat_id, conversation_id, status


async def _cancel_chat(
    context: PluginContextProtocol,
    *,
    chat_id: str,
    conversation_id: str,
    headers: dict[str, str],
    request_kwargs: dict[str, Any],
) -> None:
    """超时后尽力取消远端任务，取消失败不覆盖原始超时结果。"""

    try:
        await asyncio.wait_for(
            _request_coze_json(
                context,
                "POST",
                COZE_CANCEL_URL,
                headers=headers,
                request_kwargs={
                    **request_kwargs,
                    "timeout": _CANCEL_TIMEOUT_SECONDS,
                    "params": {"conversation_id": conversation_id, "chat_id": chat_id},
                },
            ),
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="chat.coze_cancel",
        )


async def _poll_coze_chat(
    context: PluginContextProtocol,
    state: tuple[str, str, str],
    *,
    headers: dict[str, str],
    request_kwargs: dict[str, Any],
    deadline: float,
) -> tuple[str, str, str] | None:
    """按官方 v3 非流式流程轮询，直到进入终态或超时。"""

    chat_id, conversation_id, status = state
    while status in _PENDING_CHAT_STATUSES:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError

        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
        response = await _request_coze_json_before_deadline(
            context,
            "GET",
            COZE_RETRIEVE_URL,
            headers=headers,
            request_kwargs={
                **request_kwargs,
                "params": {"conversation_id": conversation_id, "chat_id": chat_id},
            },
            deadline=deadline,
        )
        if response is None:
            return None
        next_state = _chat_state(response)
        if next_state is None:
            context.logger.error("Coze API 轮询响应缺少有效对话状态")
            return None
        chat_id, conversation_id, status = next_state
    return chat_id, conversation_id, status


async def _fetch_coze_messages(
    context: PluginContextProtocol,
    *,
    chat_id: str,
    conversation_id: str,
    headers: dict[str, str],
    request_kwargs: dict[str, Any],
    deadline: float,
) -> list[Any] | None:
    """读取一次已完成对话的消息列表。"""

    response = await _request_coze_json_before_deadline(
        context,
        "GET",
        COZE_MESSAGES_URL,
        headers=headers,
        request_kwargs={
            **request_kwargs,
            "params": {"conversation_id": conversation_id, "chat_id": chat_id},
        },
        deadline=deadline,
    )
    if response is None:
        return None
    messages = response.get("data")
    if not isinstance(messages, list):
        context.logger.error("Coze API 消息响应缺少列表数据")
        return None
    return messages


async def call_coze_api(
    query: str,
    config: Mapping[str, Any],
    context: PluginContextProtocol,
    actor_id: Any = None,
) -> dict[str, Any] | None:
    """通过 Coze v3 创建对话、轮询完成状态并读取回答消息。"""

    is_valid, error_message = validate_config(config)
    if not is_valid:
        context.logger.error("Chat 插件配置无效: %s", error_message)
        return None

    token = config["token"]
    bot_id = config["bot_id"]
    _, actor = _actor_identity(actor_id)
    identity_source = f"{bot_id}:{actor if actor is not None else _ANONYMOUS_ACTOR}"
    user_id = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:32]
    proxy = config.get("proxy") or None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "bot_id": bot_id,
        "user_id": user_id,
        "stream": False,
        # Coze v3 的非流式轮询和消息列表接口要求保存本轮历史；不传
        # conversation_id，因此各条 /chat 命令仍是彼此隔离的单轮对话。
        "auto_save_history": True,
        "additional_messages": [
            {"role": "user", "content": query, "content_type": "text"},
        ],
    }
    base_request_kwargs: dict[str, Any] = {"timeout": REQUEST_TIMEOUT}
    if proxy:
        base_request_kwargs["proxy"] = proxy
        context.logger.debug("使用已配置代理")

    state: tuple[str, str, str] | None = None
    try:
        context.logger.info("调用 Coze API v3，查询长度: %d 字符", len(query))
        async with _API_SEMAPHORE:
            deadline = asyncio.get_running_loop().time() + REQUEST_TIMEOUT
            created = await _request_coze_json_before_deadline(
                context,
                "POST",
                COZE_API_URL,
                headers=headers,
                request_kwargs={**base_request_kwargs, "json": payload},
                deadline=deadline,
            )
            if created is None:
                return None

            state = _chat_state(created)
            if state is None:
                context.logger.error("Coze API 创建响应缺少有效对话状态")
                return None
            state = await _poll_coze_chat(
                context,
                state,
                headers=headers,
                request_kwargs=base_request_kwargs,
                deadline=deadline,
            )
            if state is None:
                return None
            chat_id, conversation_id, status = state

            if status != "completed":
                context.logger.error("Coze API 对话未完成: status=%s", status)
                return None

            messages = await _fetch_coze_messages(
                context,
                chat_id=chat_id,
                conversation_id=conversation_id,
                headers=headers,
                request_kwargs=base_request_kwargs,
                deadline=deadline,
            )
            if messages is None:
                return None
            context.logger.info("Coze API 调用成功，响应消息数: %d", len(messages))
            return {"messages": messages}

    except asyncio.TimeoutError:
        context.logger.error("Coze API 请求超时 (%ss)", REQUEST_TIMEOUT)
        if state is not None and state[2] in _PENDING_CHAT_STATUSES:
            await _cancel_chat(
                context,
                chat_id=state[0],
                conversation_id=state[1],
                headers=headers,
                request_kwargs=base_request_kwargs,
            )
        return None
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="chat.coze_api",
        )
        return None


def extract_answer(data: object, context: PluginContextProtocol) -> str | None:
    """返回第一条非空文本答案；忽略畸形或非答案消息。"""

    if not isinstance(data, Mapping):
        context.logger.error("API 响应格式错误: expected=mapping actual=%s", type(data).__name__)
        return None

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        context.logger.error("messages 字段格式错误: actual=%s", type(messages).__name__)
        return None

    for message in messages:
        if not isinstance(message, Mapping) or message.get("type") != "answer":
            continue
        content = message.get("content")
        if isinstance(content, str) and (answer := content.strip()):
            context.logger.debug("提取到答案，长度: %d 字符", len(answer))
            return answer

    # 不记录服务端提供的消息类型或正文，避免外部文本注入日志。
    context.logger.warning("未找到有效文本答案，消息总数: %d", len(messages))
    return None


# ============================================================
# 命令处理
# ============================================================


async def _answer_query(
    query: str,
    event: Mapping[str, Any],
    context: PluginContextProtocol,
    config: Mapping[str, Any],
    *,
    per_user_limit: int,
    global_limit: int,
) -> list[dict[str, Any]]:
    """预留额度、调用远端并在任何失败路径回滚额度。"""

    actor, api_actor = _actor_identity(event.get("user_id"))
    try:
        reservation = await _reserve_quota(
            context,
            actor=actor,
            per_user_limit=per_user_limit,
            global_limit=global_limit,
        )
    except ChatQuotaExceeded:
        return segments("❌ 今日 AI 对话额度已用完")
    except ChatQuotaStateError:
        context.logger.error("Chat quota state is unavailable or invalid")
        return segments("❌ 今日 AI 对话额度状态异常，请联系管理员")

    try:
        data = await call_coze_api(query, config, context, api_actor)
        if data is None:
            return segments("❌ AI 对话失败，请稍后重试")

        answer = extract_answer(data, context)
        if answer is None:
            return segments("❌ 未能获取到有效回答")

        reservation.commit()
        return segments(answer)
    finally:
        if reservation.active:
            await asyncio.shield(reservation.rollback())


async def _run_chat_query(
    query: str,
    event: Mapping[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """验证查询与配置，并在持久额度事务中调用远端。"""

    settings = context.get_settings_snapshot()
    config = settings.plugin_secrets("chat")
    is_valid, error_msg = validate_config(config)
    if not is_valid:
        context.logger.error("Chat 插件配置无效: %s", error_msg)
        return segments(f"❌ 配置错误: {error_msg}")

    quota_config = settings.plugin_config("chat")
    try:
        per_user_limit = _quota_limit(
            quota_config,
            "daily_user_limit",
            DEFAULT_DAILY_USER_LIMIT,
        )
        global_limit = _quota_limit(
            quota_config,
            "daily_global_limit",
            DEFAULT_DAILY_GLOBAL_LIMIT,
        )
    except ValueError as exc:
        context.logger.error("Chat 插件额度配置无效: %s", exc)
        return segments(f"❌ 配置错误: {exc}")

    context.logger.info("Chat query accepted: length=%d 字符", len(query))
    return await _answer_query(
        query,
        event,
        context,
        config,
        per_user_limit=per_user_limit,
        global_limit=global_limit,
    )


async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """处理框架命令；`command` 保留为统一插件入口契约。"""
    try:
        query = args.strip()
        if not query:
            return segments("💬 请输入要对话的内容\n\n用法: /chat <你的问题>")

        # help 是公开子命令：精确调用显示帮助，后接参数则明确报错，不能把
        # 清单中的非法示例继续发送到远端并消耗额度。
        query_parts = query.split(maxsplit=1)
        if query_parts[0].casefold() in {"help", "帮助"}:
            if len(query_parts) == 1:
                return segments(HELP_TEXT)
            return segments("❌ help 子命令不接受额外参数\n用法: /chat help")

        # 限制查询长度
        if len(query) > MAX_QUERY_LENGTH:
            return segments(
                f"❌ 查询内容过长（{len(query)} 字符），最多支持 {MAX_QUERY_LENGTH} 字符"
            )

        return await _run_chat_query(query, event, context)

    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=context.logger,
            component="chat.handle",
        )


async def reply(
    text: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
) -> list[dict[str, Any]]:
    """供 smalltalk 声明调用的最小服务入口，不暴露命令或任意回调名。"""

    query = text.strip()
    if not query:
        return segments("💬 请输入要对话的内容")
    if len(query) > MAX_QUERY_LENGTH:
        return segments(f"❌ 查询内容过长，最多支持 {MAX_QUERY_LENGTH} 字符")
    return await _run_chat_query(query, event, context)
