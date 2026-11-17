"""插件公开入口的意外错误：一行回复加一条 ``public_error`` 日志。

回复写成 ``操作失败：原因（异常类型 @ 出错位置；request_id：…）``。原因按异常链分类：超时、
网络、上游 HTTP 状态、上游数据无法解析、模型服务、缺少依赖、数据库、文件或系统，其余算插件
内部错误；每类说明重试有没有用。类型和位置取决定分类的那一环。

异常消息可能带正文、路径或凭据，回复和日志都不写，只写类型、位置和状态码一类的属性
（见 :func:`core.log.summarize_exception`）。日志和回复带同一个 request id；日志写失败不影响回复。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from .delivery import CommandOutcome, DeliverySegments
from .log import (
    ExceptionSummary,
    LogEvent,
    _attribute,
    current_request_id,
    exception_chain,
    new_request_id,
    summarize_exception,
)

# 每条公开错误回复的开头；回复只经文本传出时（如命令矩阵）靠它认出公开错误。
PUBLIC_ERROR_PREFIX = "操作失败："

_SAFE_PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_SAFE_REQUEST_ID  = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")

# 按顺序匹配异常类 MRO 里的限定名，不导入可选库。子类在前：超时和连接错误都是 OSError，
# aiohttp 的 ContentTypeError 是 ClientResponseError，urllib 的 HTTPError 是 URLError。
_CATEGORY_TYPES: tuple[tuple[str, frozenset[str]], ...] = (
    ("model", frozenset({"core.ai.AIError"})),
    (
        "timeout",
        frozenset(
            {
                "builtins.TimeoutError",
                "requests.exceptions.Timeout",
                "subprocess.TimeoutExpired",
            }
        ),
    ),
    (
        "bad_response",
        frozenset(
            {
                "core.bounded_http.ResponseFormatError",
                "core.bounded_http.ResponseLimitError",
                "core.bounded_http.RedirectPolicyError",
                "core.safe_http.SafeHttpError",
                "aiohttp.client_exceptions.ContentTypeError",
                "requests.exceptions.InvalidJSONError",
                "requests.exceptions.ContentDecodingError",
                "requests.exceptions.TooManyRedirects",
                "json.decoder.JSONDecodeError",
            }
        ),
    ),
    (
        "upstream_status",
        frozenset(
            {
                "core.bounded_http.HttpStatusError",
                "aiohttp.client_exceptions.ClientResponseError",
                "requests.exceptions.HTTPError",
                "urllib.error.HTTPError",
            }
        ),
    ),
    (
        "network",
        frozenset(
            {
                "builtins.ConnectionError",
                "core.bounded_http.ResponseTransportError",
                "aiohttp.client_exceptions.ClientError",
                "requests.exceptions.RequestException",
                "urllib.error.URLError",
                "socket.gaierror",
                "ssl.SSLError",
                "paramiko.ssh_exception.SSHException",
                "paramiko.ssh_exception.NoValidConnectionsError",
            }
        ),
    ),
    ("dependency", frozenset({"builtins.ImportError"})),
    ("database", frozenset({"sqlite3.Error"})),
    ("io", frozenset({"builtins.OSError", "subprocess.SubprocessError"})),
)

# 只转述下层错误的包装类型：链里更深处有能分类的异常时以那一环为准，没有时才用自己的分类。
_WRAPPER_TYPES = frozenset(
    {"core.safe_http.SafeHttpError", "core.bounded_http.ResponseTransportError"}
)

_REASONS = {
    "timeout"     : "请求超时，请稍后重试",
    "network"     : "网络连接失败，请稍后重试",
    "bad_response": "上游返回的数据无法解析，可稍后重试",
    "io"          : "文件或系统操作失败，请联系管理员",
    "internal"    : "插件内部错误，重试无效，请联系管理员",
}

# ``core.ai.AIRequestError.category`` → 原因；表里没有的类别用 ``_MODEL_DEFAULT_REASON``。
_MODEL_REASONS = {
    "timeout"          : "模型服务超时，请稍后重试",
    "request_timeout"  : "模型服务超时，请稍后重试",
    "route_timeout"    : "模型服务超时，请稍后重试",
    "rate_limit"       : "模型服务限流，请稍后重试",
    "transport"        : "模型服务连接失败，请稍后重试",
    "server_error"     : "模型服务出错，请稍后重试",
    "conflict"         : "模型服务出错，请稍后重试",
    "invalid_response" : "模型返回的内容无法解析，请稍后重试",
    "empty_response"   : "模型没有返回内容，请稍后重试",
    "authentication"   : "模型服务鉴权失败，请联系管理员",
    "model_unavailable": "模型不可用，请联系管理员",
    "invalid_request"  : "模型服务拒绝了请求，请联系管理员",
    "request_too_large": "请求内容超出模型上限，重试无效",
}
_MODEL_DEFAULT_REASON  = "模型服务调用失败，请稍后重试"
_MODEL_CONFIG_REASON   = "模型配置无效，请联系管理员"
_RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429})


def _type_names(exc: BaseException) -> frozenset[str]:
    return frozenset(f"{kind.__module__}.{kind.__qualname__}" for kind in type(exc).__mro__)


def _link_category(names: frozenset[str]) -> str | None:
    for category, types in _CATEGORY_TYPES:
        if names & types:
            return category
    return None


def _classify(exc: BaseException) -> tuple[str, BaseException]:
    """从外到里找第一个能分类的异常，返回类别和那一环；都不能分类时是 ``internal`` 和 ``exc``。"""

    wrapped: tuple[str, BaseException] | None = None
    for link in exception_chain(exc):
        names    = _type_names(link)
        category = _link_category(names)
        if category is None:
            continue
        if not names & _WRAPPER_TYPES:
            return category, link
        wrapped = wrapped or (category, link)
    return wrapped or ("internal", exc)


def _http_status_reason(status: object) -> str:
    if not isinstance(status, int):
        return "上游服务返回错误状态"
    if status in _RETRYABLE_HTTP_STATUS or status >= 500:
        return f"上游服务返回 HTTP {status}，请稍后重试"
    return f"上游服务返回 HTTP {status}，重试无效"


def _model_reason(link: BaseException) -> tuple[str, str | None]:
    if "core.ai.AIConfigError" in _type_names(link):
        return _MODEL_CONFIG_REASON, None
    model_category = _attribute(link, "category")
    if not isinstance(model_category, str):
        return _MODEL_DEFAULT_REASON, None
    return _MODEL_REASONS.get(model_category, _MODEL_DEFAULT_REASON), model_category


def _reason(
    category: str, link: BaseException, details: Mapping[str, int | str]
) -> tuple[str, str | None]:
    """回复里的原因，和日志的 ``reason_code``（模型请求错误的类别，其余为 None）。"""

    if category == "model":
        return _model_reason(link)
    if category == "upstream_status":
        return _http_status_reason(details.get("status")), None
    if category == "dependency":
        module = details.get("module")
        if module:
            return f"缺少依赖 {module}，请联系管理员", None
        return "缺少依赖模块，请联系管理员", None
    if category == "database":
        sqlite_name = str(details.get("sqlite", ""))
        if sqlite_name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
            return "数据库正忙，请稍后重试", None
        return "数据库操作失败，请联系管理员", None
    return _REASONS[category], None


def _request_id(context: object) -> str:
    candidate = _attribute(context, "request_id")
    if isinstance(candidate, str) and _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return current_request_id() or new_request_id()


def _logger(context: object) -> logging.Logger:
    plugin_name = _attribute(context, "plugin_name")
    if isinstance(plugin_name, str) and _SAFE_PLUGIN_NAME.fullmatch(plugin_name):
        return logging.getLogger(f"plugins.{plugin_name}")
    return logging.getLogger(__name__)


def _log_public_error(
    context: object,
    exc: BaseException,
    *,
    component: str,
    category: str,
    reason_code: str | None,
    request_id: str,
) -> None:
    try:
        _logger(context).error(
            LogEvent(
                "public_error",
                component   = component,
                category    = category,
                reason_code = reason_code,
                error       = exc,
            ),
            exc_info = exc,
            extra    = {"request_id": request_id},
        )
    except Exception:
        # 日志出口坏了也照常回复。
        return


def _where(summary: ExceptionSummary, exc: BaseException) -> str:
    location = summary.location or summarize_exception(exc).location
    return f"{summary.type_name} @ {location}" if location else summary.type_name


def public_error_message(context: object, exc: BaseException, *, component: str) -> str:
    """记一条 ``public_error`` 日志，返回给用户的一行说明（原因、异常类型、位置和 request id）。"""

    request_id = _request_id(context)
    category, link = _classify(exc)
    summary = summarize_exception(link)
    reason, reason_code = _reason(category, link, summary.details)
    _log_public_error(
        context,
        exc,
        component   = component,
        category    = category,
        reason_code = reason_code,
        request_id  = request_id,
    )
    return f"{PUBLIC_ERROR_PREFIX}{reason}（{_where(summary, exc)}；request_id：{request_id}）"


def public_error_response(
    context: object,
    exc: BaseException,
    *,
    component: str,
) -> DeliverySegments:
    """:func:`public_error_message` 的文本做成一段 OneBot 回复，结果是 ``CommandOutcome("error")``。"""

    message = public_error_message(context, exc, component=component)
    return DeliverySegments(
        [{"type": "text", "data": {"text": message}}], outcome=CommandOutcome("error")
    )


def log_public_error(context: object, exc: BaseException, *, component: str) -> None:
    """只记 ``public_error`` 日志、不回复：失败不该打扰用户的地方（后台任务、静默降级）用它。"""

    public_error_message(context, exc, component=component)


__all__ = [
    "PUBLIC_ERROR_PREFIX",
    "log_public_error",
    "public_error_message",
    "public_error_response",
]
