"""Safe, correlated error responses for public plugin entry points.

Public handlers must not expose exception text to QQ users.  This module keeps
the public contract deliberately small while retaining enough, bounded and
redacted diagnostic information for an operator to correlate the failure.

面向 QQ 的返回值只包含固定错误码和经校验的 request_id；异常文本、链路和栈帧只进入
有界诊断日志。密钥扫描一旦因深度、数量或异常对象而不完整，就整体省略异常消息，
不能用“尽量脱敏”的结果冒险；日志系统自身失败也不得改变安全的用户响应。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence, Set
from typing import Any

PUBLIC_ERROR_CODE = "XQ-PLUGIN-UNEXPECTED"
PUBLIC_ERROR_TEXT = "操作失败，请稍后重试"

_MAX_REQUEST_ID_CHARS = 64
_MAX_COMPONENT_CHARS = 96
_MAX_RAW_CHARS = 32_768
_MAX_MESSAGE_CHARS = 4_096
_MAX_TRACEBACK_CHARS = 12_288
_MAX_SECRET_DEPTH = 8
_MAX_SECRET_VALUES = 512
_MAX_EXCEPTION_CHAIN = 8
_MAX_TRACEBACK_FRAMES = 48

_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_AUTHORIZATION = re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;]+")
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;]+")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
    r"\s*[:=]\s*[^\s,;]+"
)
_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s\"'<>]+")
_UNC_PATH = re.compile(r"(?<![\\\w])\\\\[^\\/\s\"'<>|]+[\\/][^\s\"'<>|]+")
_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/])[^\s\"'<>|]+")
_POSIX_PATH = re.compile(r"(?<![\w:])/(?!/)(?:[^/\s\"'<>|]+/)*[^/\s\"'<>|:,;)\]}]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe_getattr(value: object, name: str, default: Any = None) -> Any:
    try:
        return getattr(value, name, default)
    except BaseException:
        return default


def _bounded_string(value: object, *, limit: int = _MAX_RAW_CHARS) -> str | None:
    try:
        text = str(value)
    except BaseException:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}<truncated>"


def _bounded_exception_message(value: object) -> tuple[str | None, bool]:
    """Read an exception message without ever exposing a truncated credential.

    Redacting a prefix is unsafe when a secret, URL, or authorization value
    crosses the truncation boundary.  Oversized messages therefore fail closed
    as a whole while their type and bounded traceback frames remain available.
    """

    try:
        text = str(value)
    except BaseException:
        return None, False
    if len(text) > _MAX_RAW_CHARS:
        return None, True
    return text, False


def _collect_secret_strings(root: object) -> tuple[tuple[str, ...], bool]:
    """Collect bounded secret values and report whether the scan was complete.

    A truncated or unusual secret tree fails closed: callers omit all exception
    messages instead of risking a value that could not be discovered here.
    """

    if root is None:
        return (), True

    values: set[str] = set()
    seen: set[int] = set()
    stack: list[tuple[object, int]] = [(root, 0)]
    complete = True
    visited_values = 0

    while stack:
        current, depth = stack.pop()
        visited_values += 1
        if visited_values > _MAX_SECRET_VALUES:
            complete = False
            break

        if isinstance(current, str):
            if current:
                if len(current) > _MAX_RAW_CHARS:
                    complete = False
                else:
                    values.add(current)
            continue
        if isinstance(current, bytes):
            try:
                decoded = current.decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                complete = False
                continue
            if decoded:
                if len(decoded) > _MAX_RAW_CHARS:
                    complete = False
                else:
                    values.add(decoded)
            continue

        if isinstance(current, Mapping):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            if depth >= _MAX_SECRET_DEPTH:
                complete = False
                continue
            try:
                children = list(current.values())
            except BaseException:
                complete = False
                continue
            stack.extend((child, depth + 1) for child in children)
            continue

        if isinstance(current, (Sequence, Set)) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            object_id = id(current)
            if object_id in seen:
                continue
            seen.add(object_id)
            if depth >= _MAX_SECRET_DEPTH:
                complete = False
                continue
            try:
                children = list(current)
            except BaseException:
                complete = False
                continue
            stack.extend((child, depth + 1) for child in children)
            continue

        if current is not None and not isinstance(current, (bool, int, float)):
            complete = False

    return tuple(sorted(values, key=len, reverse=True)), complete


def _redact_known_secrets(text: str, secrets: tuple[str, ...]) -> str:
    if not text or not secrets:
        return text

    # Mark matches against the original text and render them in one pass.  A
    # sequence of ``str.replace`` calls is unsafe here: a later short secret
    # can match text inside the replacement marker, while the previous
    # word-boundary approach failed to cover punctuation-bearing secrets such
    # as ``a-``.  The bounded input makes this mask predictable in both memory
    # and runtime while covering overlapping matches of every non-empty value.
    redacted = bytearray(len(text))
    for secret in secrets:
        if not secret:
            continue
        start = 0
        while True:
            match = text.find(secret, start)
            if match < 0:
                break
            redacted[match : match + len(secret)] = b"\x01" * len(secret)
            start = match + 1

    if not any(redacted):
        return text

    chunks: list[str] = []
    index = 0
    while index < len(text):
        if not redacted[index]:
            next_redacted = redacted.find(1, index)
            if next_redacted < 0:
                chunks.append(text[index:])
                break
            chunks.append(text[index:next_redacted])
            index = next_redacted
            continue
        next_visible = index + 1
        while next_visible < len(text) and redacted[next_visible]:
            next_visible += 1
        chunks.append("<redacted-secret>")
        index = next_visible
    return "".join(chunks)


def _escape_control(match: re.Match[str]) -> str:
    value = ord(match.group(0))
    if value == 9:
        return r"\t"
    if value == 10:
        return r"\n"
    if value == 13:
        return r"\r"
    return f"\\x{value:02x}"


def _sanitize_text(
    text: str,
    *,
    secrets: tuple[str, ...],
    limit: int,
) -> str:
    # Never keep a prefix of oversized, untrusted text.  A credential can
    # straddle the cutoff, leaving its prefix visible before redaction.
    if len(text) > _MAX_RAW_CHARS:
        return "<omitted: oversized diagnostic>"
    text = _redact_known_secrets(text, secrets)
    text = _AUTHORIZATION.sub("Authorization: <redacted-credential>", text)
    text = _AUTH_SCHEME.sub(lambda match: f"{match.group(1)} <redacted-credential>", text)
    text = _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=<redacted-credential>",
        text,
    )
    text = _URL.sub("<redacted-url>", text)
    text = _UNC_PATH.sub("<redacted-path>", text)
    text = _WINDOWS_PATH.sub("<redacted-path>", text)
    text = _POSIX_PATH.sub("<redacted-path>", text)
    text = _CONTROL.sub(_escape_control, text)
    if len(text) > limit:
        return f"{text[:limit]}<truncated>"
    return text


def _secret_conflicts_with_request_id(request_id: str, secrets: tuple[str, ...]) -> bool:
    request_id_folded = request_id.casefold()
    return any(len(secret) >= 4 and secret.casefold() in request_id_folded for secret in secrets)


def _normalize_request_id(value: object, secrets: tuple[str, ...]) -> str:
    if value is None:
        return uuid.uuid4().hex[:12]
    candidate = _bounded_string(value, limit=_MAX_REQUEST_ID_CHARS)
    if (
        candidate is not None
        and _SAFE_REQUEST_ID.fullmatch(candidate)
        and not _secret_conflicts_with_request_id(candidate, secrets)
    ):
        return candidate
    return uuid.uuid4().hex[:12]


def _normalize_component(value: object, secrets: tuple[str, ...]) -> str:
    candidate = _bounded_string(value, limit=_MAX_COMPONENT_CHARS)
    if candidate is None or not _SAFE_COMPONENT.fullmatch(candidate):
        return "unknown"
    sanitized = _redact_known_secrets(candidate, secrets)
    if sanitized != candidate:
        return "unknown"
    return candidate


def _exception_type_name(exc: BaseException, secrets: tuple[str, ...]) -> str:
    exception_type = type(exc)
    module = _bounded_string(_safe_getattr(exception_type, "__module__", "builtins"), limit=128)
    name = _bounded_string(
        _safe_getattr(exception_type, "__qualname__", "BaseException"), limit=256
    )
    raw_name = f"{module or 'builtins'}.{name or 'BaseException'}"
    return _sanitize_text(raw_name, secrets=secrets, limit=384)


def _base_exception_attribute(exc: BaseException, name: str, default: Any = None) -> Any:
    try:
        return BaseException.__getattribute__(exc, name)
    except BaseException:
        return default


def _traceback_frames(exc: BaseException, secrets: tuple[str, ...]) -> tuple[list[str], bool]:
    current = _base_exception_attribute(exc, "__traceback__")
    frames: list[str] = []
    seen: set[int] = set()
    truncated = False

    while current is not None:
        current_id = id(current)
        if current_id in seen:
            truncated = True
            break
        seen.add(current_id)
        if len(frames) >= _MAX_TRACEBACK_FRAMES:
            truncated = True
            break
        try:
            code = current.tb_frame.f_code
            filename = _bounded_string(code.co_filename, limit=1_024) or "<unknown>"
            function = _bounded_string(code.co_name, limit=256) or "<unknown>"
            lineno = int(current.tb_lineno)
            frame_text = f'File "{filename}", line {lineno}, in {function}'
            frames.append(_sanitize_text(frame_text, secrets=secrets, limit=1_536))
            current = current.tb_next
        except BaseException:
            frames.append("<unavailable traceback frame>")
            truncated = True
            break

    return frames, truncated


def _exception_chain(
    exc: BaseException, secrets: tuple[str, ...], *, include_messages: bool
) -> dict[str, Any]:
    current: BaseException | None = exc
    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    relation = "root"
    chain_truncated = False

    while current is not None:
        current_id = id(current)
        if current_id in seen or len(entries) >= _MAX_EXCEPTION_CHAIN:
            chain_truncated = True
            break
        seen.add(current_id)

        message, message_oversized = _bounded_exception_message(current)
        # 密钥树未完整扫描时，整条异常链都不记录消息；不能只省略当前节点，
        # 因为同一凭据可能出现在 cause/context 的任意一层。
        if not include_messages:
            safe_message = "<omitted: secret scan limit>"
        elif message_oversized:
            safe_message = "<omitted: oversized exception>"
        elif message is None:
            safe_message = "<unprintable exception>"
        else:
            safe_message = _sanitize_text(
                message,
                secrets=secrets,
                limit=_MAX_MESSAGE_CHARS,
            )

        frames, frames_truncated = _traceback_frames(current, secrets)
        entries.append(
            {
                "relation": relation,
                "type": _exception_type_name(current, secrets),
                "message": safe_message,
                "frames": frames,
                "frames_truncated": frames_truncated,
            }
        )

        cause = _base_exception_attribute(current, "__cause__")
        if isinstance(cause, BaseException):
            current = cause
            relation = "cause"
            continue
        suppressed = bool(_base_exception_attribute(current, "__suppress_context__", False))
        context = _base_exception_attribute(current, "__context__")
        if not suppressed and isinstance(context, BaseException):
            current = context
            relation = "context"
            continue
        current = None

    root = entries[0]
    traceback_text = " | ".join(
        f"{entry['relation']} {entry['type']}: " + "; ".join(entry["frames"]) for entry in entries
    )
    if len(traceback_text) > _MAX_TRACEBACK_CHARS:
        traceback_text = f"{traceback_text[:_MAX_TRACEBACK_CHARS]}<truncated>"
        chain_truncated = True
    return {
        "exception_type": root["type"],
        "exception_message": root["message"],
        "exception_chain": entries,
        "traceback": traceback_text,
        "chain_truncated": chain_truncated,
    }


def _log_public_error(logger: object, payload: dict[str, Any], request_id: str) -> None:
    selected_logger = logger
    # PluginContext's request-aware adapter stores its real stdlib logger here.
    # Unwrapping it prevents an untrusted context request ID from overriding the
    # normalized value supplied below.
    base_logger = _safe_getattr(selected_logger, "_base_logger")
    if isinstance(base_logger, logging.Logger):
        selected_logger = base_logger
    error_method = _safe_getattr(selected_logger, "error")
    if not callable(error_method):
        return
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    try:
        # 只传入已经序列化、经过边界处理的 payload；绝不把原始 exc 交给
        # logger.exception 或 exc_info，以免格式化器重新泄漏路径和异常文本。
        error_method(
            "public_error %s",
            serialized,
            extra={"request_id": request_id},
        )
    except BaseException:
        # A logging transport failure must not replace the safe user response.
        return


def public_error_message(
    context: object,
    exc: BaseException,
    *,
    logger: object,
    component: str,
) -> str:
    """Log one bounded diagnostic record and return a stable public message."""

    secret_values, secret_scan_complete = _collect_secret_strings(_safe_getattr(context, "secrets"))
    request_id = _normalize_request_id(
        _safe_getattr(context, "request_id"),
        secret_values,
    )
    safe_component = _normalize_component(component, secret_values)
    diagnostic = _exception_chain(
        exc,
        secret_values,
        include_messages=secret_scan_complete,
    )
    payload: dict[str, Any] = {
        "event": "public_error",
        "error_code": PUBLIC_ERROR_CODE,
        "request_id": request_id,
        "component": safe_component,
        "secret_scan_complete": secret_scan_complete,
        **diagnostic,
    }
    _log_public_error(logger, payload, request_id)
    return f"{PUBLIC_ERROR_TEXT}（错误码：{PUBLIC_ERROR_CODE}；request_id：{request_id}）"


def public_error_response(
    context: object,
    exc: BaseException,
    *,
    logger: object,
    component: str,
) -> list[dict[str, Any]]:
    """Return a OneBot text action containing only the safe public message."""

    message = public_error_message(
        context,
        exc,
        logger=logger,
        component=component,
    )
    return [{"type": "text", "data": {"text": message}}]


__all__ = [
    "PUBLIC_ERROR_CODE",
    "PUBLIC_ERROR_TEXT",
    "public_error_message",
    "public_error_response",
]
