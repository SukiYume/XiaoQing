"""日志：request id 关联、事件脱敏渲染、统一格式与轮转，以及进程级的异常出口。

插件只用标准库 ``logging.getLogger(__name__)`` 加 :class:`LogEvent` 写日志；request id、
脱敏、格式和级别配置都在这里。``LogEvent`` 在构造时就把字段脱敏并渲染成文本，对象里
不留原始值，所以格式化失败时 ``logging`` 打印 ``record.msg`` 也不会泄漏正文。

进程入口先调 :func:`bootstrap_logging`（只开控制台，接管未捕获异常的出口），读到配置后
:func:`setup_logging` 换成配置的出口；事件循环的异常处理器是 :func:`asyncio_exception_handler`。
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import datetime
import hashlib
import hmac
import json
import logging
import numbers
import os
import re
import secrets
import sys
import sysconfig
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, ClassVar, Literal, NamedTuple, TextIO

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

LevelName = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_STD_OUTPUT_HANDLE                  = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_LOG_FILE_NAME  = "xiaoqing.log"
_DATE_FORMAT    = "%Y-%m-%d %H:%M:%S"
_CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s"
_FILE_FORMAT    = (
    "%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
)
_MAX_MESSAGE_CHARS   = 4000
_MAX_TRACEBACK_CHARS = 8000
_PROJECT_ROOT        = Path(__file__).resolve().parent.parent
_STDLIB_ROOT         = Path(sysconfig.get_paths()["stdlib"])
# 项目自己的 logger（``__main__``、``main`` 是进程入口）：``logging.level`` 管它们，它们不用
# LogEvent 的记录照常格式化。其余都是第三方库：默认 WARNING，``level`` 更严时跟着更严，
# 记录的格式化参数按字段规则脱敏。
_PROJECT_LOGGERS    = ("core", "plugins", "scripts", "main", "__main__")
_OTHER_LOGGER_LEVEL = logging.WARNING
# 同一出口同一阶段持续写不出去时，诊断每分钟最多一行。
_OUTPUT_FAILURE_INTERVAL_S = 60.0

# 进程随机密钥：进程内同一段原文的指纹能对上，重启即换，指纹拿不去离线撞库。
_FINGERPRINT_KEY   = secrets.token_bytes(32)
_MAX_FLATTEN_DEPTH = 6
_TOKEN_RE          = re.compile(r"[A-Za-z0-9_.:/@+-]{1,128}")
_PLAIN_NAME_RE     = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NEEDS_QUOTE_RE    = re.compile(r"[\s\"'=]")
_KEY_UNSAFE_RE     = re.compile(r"[^A-Za-z0-9_.-]")
_KEY_PART_RE       = re.compile(r"[_.]")
_CONTROL_RE        = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

_CREDENTIAL_PARTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "cookie",
        "auth",
        "authorization",
        "credential",
        "apikey",
        "key",
    }
)
_BODY_NAMES = frozenset({"name", "nickname", "sender_name", "user_name", "display_name", "card"})
_BODY_PARTS = frozenset(
    {
        "text",
        "content",
        "message",
        "msg",
        "history",
        "dialogue",
        "reply",
        "prompt",
        "response",
        "raw",
        "output",
        "input",
        "thinking",
        "reason",
        "goal",
        "hint",
        "description",
        "marker",
        "term",
        "emotion",
        "summary",
        "evidence",
        "path",
        "url",
        "uri",
        "host",
        "payload",
        "command",
        "cmd",
        "query",
        "title",
        "preview",
        "body",
        "detail",
    }
)
_PLAIN_KEY_SUFFIXES = ("_id", "_code", "_count", "_kind", "_type", "_status", "_name")


class LoggingSettings(BaseModel):
    """配置文件 ``logging`` 块的唯一定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level    : LevelName = "INFO"
    levels   : dict[StrictStr, LevelName] = Field(default_factory=dict)
    console  : StrictBool = True
    file     : StrictBool = True
    keep_days: int = Field(default=5, ge=1, le=365)


# ============================================================
# request id 关联
# ============================================================

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "xiaoqing_log_request_id", default=None
)


@contextmanager
def log_scope(request_id: str | None) -> Iterator[None]:
    """在块内让日志带上 ``request_id``；传 ``None`` 表示清空，退出时恢复进入前的值。"""

    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def new_request_id() -> str:
    """新的 8 位十六进制 request id：一条入站消息、一次定时任务运行各一个。"""

    return uuid.uuid4().hex[:8]


def current_request_id() -> str | None:
    """当前 :func:`log_scope` 设置的 request id；不在任何 scope 内时为 ``None``。"""

    return _request_id.get()


class _RequestIdFilter(logging.Filter):
    """记录上没有 request_id 时补当前 scope 的；显式 ``extra={"request_id": ...}`` 优先。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or "-"
        return True


# ============================================================
# 异常：类型、位置和状态码，不含异常消息
# ============================================================

_MAX_EXCEPTION_CHAIN  = 8
_MAX_TRACEBACK_FRAMES = 48
# 位置（``路径:行``）的前缀。插件代码那一帧最能说明是哪一步出的错：同一个 Core 工具函数被
# 很多插件调用，它自己的行号分不出是谁。
_PLUGIN_CODE_PREFIX    = "plugins/"
_PROJECT_CODE_PREFIXES = ("core/", "scripts/", "main.py:")
_INT_DETAIL_ATTRS      = ("status", "errno", "winerror", "returncode", "lineno", "colno")


class ExceptionSummary(NamedTuple):
    """一个异常里能安全写出去的部分；异常消息可能带正文、路径或凭据，不在其中。

    作为 :class:`LogEvent` 的字段值时和异常本身渲染成一样的字段。
    """

    type_name: str
    # 回溯里最深一帧插件代码（plugins/）的 ``文件:行``；没有插件代码时取最深一帧项目内其他代码
    # （core/、scripts/、main.py），再没有取最深一帧真实文件，``<frozen …>``、``<string>`` 这类
    # 只在全是它们时才取；没有回溯时为 None。
    location: str | None
    # 异常链最里层那个异常的类型名；没有 ``__cause__`` / 未抑制的 ``__context__`` 时为 None。
    cause: str | None
    # 状态码一类的属性：``status``（HTTP）、``errno``、``winerror``、``returncode``、
    # ``lineno`` / ``colno``（解析错误的行列号）、``sqlite``（错误名）、``module``（导入失败的模块名）。
    details: Mapping[str, int | str]


def _frame_path(filename: str) -> str:
    """回溯里的文件写成项目内、包内或标准库内的相对路径，不带本机目录。"""

    path = Path(filename)
    try:
        return path.relative_to(_PROJECT_ROOT).as_posix()
    except ValueError:
        pass
    # 先认 site-packages：conda、venv 的 site-packages 可能就在标准库目录下面。
    if "site-packages" in path.parts:
        return "/".join(path.parts[path.parts.index("site-packages") + 1 :])
    try:
        return path.relative_to(_STDLIB_ROOT).as_posix()
    except ValueError:
        return path.name or "<unknown>"


def _traceback_frames(traceback: TracebackType | None) -> list[tuple[str, int | None, str]]:
    """回溯从外到里每一帧的 ``(文件, 行, 函数)``；不读源码，也不碰异常消息。"""

    frames: list[tuple[str, int | None, str]] = []
    seen  : set[int]                          = set()
    while traceback is not None and id(traceback) not in seen:
        seen.add(id(traceback))
        code = traceback.tb_frame.f_code
        frames.append((code.co_filename, traceback.tb_lineno, code.co_name))
        traceback = traceback.tb_next
    return frames


def _attribute(value: object, name: str) -> Any:
    """读属性；属性可能是会抛异常的 property，读不到就当没有。"""

    try:
        return getattr(value, name, None)
    except BaseException:
        return None


def _exception_location(exc: BaseException) -> str | None:
    """出错位置：按插件代码、项目内其他代码、真实文件、其余的顺序，取最靠前那一档里最深的一帧。"""

    deepest: dict[int, str] = {}
    frames                  = _traceback_frames(_attribute(exc, "__traceback__"))
    for filename, line, _function in reversed(frames):
        location = f"{_frame_path(filename)}:{line}"
        if location.startswith(_PLUGIN_CODE_PREFIX):
            return location
        if location.startswith(_PROJECT_CODE_PREFIXES):
            rank = 1
        else:
            rank = 3 if filename.startswith("<") else 2
        deepest.setdefault(rank, location)
    return deepest[min(deepest)] if deepest else None


def _exception_details(exc: BaseException) -> dict[str, int | str]:
    details: dict[str, int | str] = {}
    for name in _INT_DETAIL_ATTRS:
        value = _attribute(exc, name)
        if isinstance(value, int) and not isinstance(value, bool):
            details[name] = int(value)
    if "status" not in details:
        # requests / httpx 的 HTTP 错误把状态码放在 ``response`` 上。
        response = _attribute(exc, "response")
        for name in ("status_code", "status"):
            value = _attribute(response, name)
            if isinstance(value, int) and not isinstance(value, bool):
                details["status"] = int(value)
                break
    sqlite_name = _attribute(exc, "sqlite_errorname")
    if isinstance(sqlite_name, str) and _TOKEN_RE.fullmatch(sqlite_name):
        details["sqlite"] = sqlite_name
    if isinstance(exc, ImportError):
        module = _attribute(exc, "name")
        if isinstance(module, str) and _TOKEN_RE.fullmatch(module):
            details["module"] = module
    return details


def exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` 和它的 ``__cause__`` / 未抑制的 ``__context__``，从外到里，最多 8 个。"""

    chain: list[BaseException] = [exc]
    while len(chain) < _MAX_EXCEPTION_CHAIN:
        current   = chain[-1]
        following = _attribute(current, "__cause__")
        if not isinstance(following, BaseException):
            if _attribute(current, "__suppress_context__"):
                break
            following = _attribute(current, "__context__")
        if not isinstance(following, BaseException) or any(following is seen for seen in chain):
            break
        chain.append(following)
    return chain


def summarize_exception(exc: BaseException) -> ExceptionSummary:
    """日志字段、回溯和公开错误消息共用的异常摘要。"""

    chain = exception_chain(exc)
    return ExceptionSummary(
        type_name = type(exc).__name__,
        location  = _exception_location(exc),
        cause     = type(chain[-1]).__name__ if len(chain) > 1 else None,
        details   = MappingProxyType(_exception_details(exc)),
    )


def _exception_type_name(exc: BaseException) -> str:
    kind   = type(exc)
    module = kind.__module__
    return kind.__qualname__ if module == "builtins" else f"{module}.{kind.__qualname__}"


_CAUSE_HEADER   = "The above exception was the direct cause of the following exception:"
_CONTEXT_HEADER = "During handling of the above exception, another exception occurred:"


def _render_exception(exc: BaseException) -> str:
    """回溯只写类型链、状态码一类的属性和调用位置，不写异常消息；每段最多留最里面的 48 帧。"""

    chain             = exception_chain(exc)
    blocks: list[str] = []
    for index in range(len(chain) - 1, -1, -1):
        current   = chain[index]
        frames    = _traceback_frames(_attribute(current, "__traceback__"))
        omitted   = max(0, len(frames) - _MAX_TRACEBACK_FRAMES)
        type_line = _exception_type_name(current)
        details   = _exception_details(current)
        if details:
            type_line += " (" + " ".join(f"{name}={value}" for name, value in details.items()) + ")"
        lines = [
            "Traceback (most recent call last; exception messages are not logged):",
            *([f"  …(+{omitted} earlier frames)"] if omitted else []),
            *(
                f"  {_frame_path(filename)}:{line} in {function}"
                for filename, line, function in frames[omitted:]
            ),
            type_line,
        ]
        blocks.append("\n".join(_CONTROL_RE.sub(_escape_control, line) for line in lines))
        if index:
            is_cause = _attribute(chain[index - 1], "__cause__") is current
            blocks.append(_CAUSE_HEADER if is_cause else _CONTEXT_HEADER)
    text = "\n\n".join(blocks)
    if len(text) > _MAX_TRACEBACK_CHARS:
        text = f"{text[:_MAX_TRACEBACK_CHARS]}…(+{len(text) - _MAX_TRACEBACK_CHARS} chars)"
    return text


# ============================================================
# 事件：构造时脱敏并渲染
# ============================================================


def _redacted(value: str | bytes | bytearray | memoryview) -> str:
    """长度加指纹：同一进程里同一段原文指纹相同，能对上是哪段；文本与它的字节指纹不同。"""

    if isinstance(value, str):
        kind, unit, payload = b"text", "ch", value.encode("utf-8", errors="surrogatepass")
        length = len(value)
    else:
        kind, unit, payload = b"bytes", "B", bytes(value)
        length = len(payload)
    digest = hmac.new(_FINGERPRINT_KEY, kind + b"\x00" + payload, hashlib.sha256).hexdigest()
    return f"<{length}{unit} #{digest[:8]}>"


def _quoted_if_needed(text: str) -> str:
    return json.dumps(text, ensure_ascii=False) if _NEEDS_QUOTE_RE.search(text) else text


def _key_parts(key: str) -> list[str]:
    return _KEY_PART_RE.split(key.lower())


def _is_credential_key(key: str) -> bool:
    return any(part in _CREDENTIAL_PARTS for part in _key_parts(key))


def _is_body_segment(segment: str) -> bool:
    if segment in _BODY_NAMES:
        return True
    if segment.endswith(_PLAIN_KEY_SUFFIXES):
        return False
    return any(part in _BODY_PARTS for part in segment.split("_"))


def _is_body_key(key: str) -> bool:
    """展平后的键只要有一段是正文键就算正文：``payload.file_name`` 跟着 ``payload`` 脱敏。"""

    return any(_is_body_segment(segment) for segment in key.lower().split("."))


def _safe_value(value: object, key: str | None = None) -> object:
    """一个值按字段规则换成能写出去的值；事件字段和第三方库日志的格式化参数共用这一套规则。

    ``None``、布尔、数字（int、IntEnum、Decimal、Fraction、numpy 标量）原样或换成普通数字，
    日期时间写 ISO 文本；字符串只有带着键名、键名不是凭据或正文键、值本身又是简单 token 时
    照写，没有键名的字符串（第三方库的位置参数）说不清是标识还是凭据，一律只记长度和指纹；
    异常只写类型名；序列、集合和映射写 ``[N items]``；字节、路径和其他对象只记长度和指纹。
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, numbers.Real):
        return float(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return f"[{len(value)} items]"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _redacted(value)
    if isinstance(value, str):
        plain_key = key is not None and not (_is_credential_key(key) or _is_body_key(key))
        return value if plain_key and _TOKEN_RE.fullmatch(value) else _redacted(value)
    if isinstance(value, os.PathLike):
        # 路径是自由文本：和同一路径的字符串指纹相同。
        return _redacted(os.fspath(value))
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = type(value).__name__
    return _redacted(text)


class _Fields:
    """按字段规则收集脱敏后的值，同时渲染成 ``k=v`` 片段；映射展开成多个字段，异常展开成摘要字段。"""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.parts: list[str]          = []

    def _put(self, key: str, value: object, rendered: str) -> None:
        self.values[key] = value
        self.parts.append(f"{key}={_quoted_if_needed(rendered)}")

    def add(self, key: str, value: object, depth: int = 0) -> None:
        try:
            self._add(key, value, depth)
        except Exception:
            self._put(key, "<unrenderable>", "<unrenderable>")

    def _add(self, key: str, value: object, depth: int) -> None:
        if value is None:
            return
        if isinstance(value, BaseException):
            self._add(key, summarize_exception(value), depth)
        elif isinstance(value, ExceptionSummary):
            self._put(key, value.type_name, value.type_name)
            if value.location is not None:
                self._put(f"{key}.at", value.location, value.location)
            if value.cause is not None:
                self._put(f"{key}.cause", value.cause, value.cause)
            for detail_name, detail in value.details.items():
                self._put(f"{key}.{detail_name}", detail, str(detail))
        elif isinstance(value, Mapping):
            if depth >= _MAX_FLATTEN_DEPTH:
                marker = _redacted(type(value).__name__)
                self._put(key, marker, marker)
                return
            for child_key, child in value.items():
                child_name = _KEY_UNSAFE_RE.sub("_", str(child_key))[:64] or "_"
                self.add(f"{key}.{child_name}", child, depth + 1)
        else:
            safe = _safe_value(value, key)
            self._put(
                key, safe, ("true" if safe else "false") if isinstance(safe, bool) else str(safe)
            )


class LogEvent:
    """一条已脱敏、已渲染的日志事件：``name`` 加若干字段。

    用法：``log.info(LogEvent("reply.done", chat_id=1, reply=text))``。

    ``fields`` 是脱敏、展平后的值，供测试和评测读取；``str()`` 与 ``repr()`` 都返回渲染
    文本，所以任何标准库格式化路径都碰不到原始值。
    """

    __slots__ = ("_text", "fields", "name")

    def __init__(self, name: str, /, **fields: object) -> None:
        collected = _Fields()
        for key, value in fields.items():
            collected.add(key, value)
        self.name                         = name
        self.fields: Mapping[str, object] = MappingProxyType(collected.values)
        self._text                        = " ".join([name, *collected.parts])

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._text


# ============================================================
# 格式与出口
# ============================================================


def _enable_windows_ansi(kernel32: Any) -> bool:
    """Enable ANSI output without discarding the console's existing mode bits."""

    import ctypes

    handle       = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    current_mode = ctypes.c_uint()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(current_mode)):
        return False
    updated_mode = current_mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return bool(kernel32.SetConsoleMode(handle, updated_mode))


def _stdout_supports_color() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return _enable_windows_ansi(ctypes.windll.kernel32)
    except Exception:
        return False


def _single_line(message: str) -> str:
    """转义换行和控制字符，超长截断；日志注入和刷屏都在这里挡住。"""

    message = _CONTROL_RE.sub(_escape_control, message)
    if len(message) > _MAX_MESSAGE_CHARS:
        extra   = len(message) - _MAX_MESSAGE_CHARS
        message = f"{message[:_MAX_MESSAGE_CHARS]}…(+{extra} chars)"
    return message


def _escape_control(match: re.Match[str]) -> str:
    char = match.group(0)
    if char == "\n":
        return "\\n"
    if char == "\r":
        return "\\r"
    return f"\\x{ord(char):02x}"


def _is_project_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _PROJECT_LOGGERS)


_ARGUMENTS_MISMATCH = "<arguments do not match the format>"


def _record_message(record: logging.LogRecord) -> str:
    """记录的消息文本。

    ``LogEvent`` 和项目自己 logger 的记录照常格式化。第三方库的记录不经过字段脱敏，参数里
    可能是请求头、URL 查询串、原始帧或对端地址：保留库代码里写死的消息模板，参数按
    :func:`_safe_value` 换掉再填进去；``%(name)s`` 形式的参数按键名套用同样的键规则。填不进去
    （例如 ``%d`` 遇到被换成标记的文本）时写模板加固定标记，不退回原始消息。没有参数的记录原样写。
    """

    args = record.args
    if not args or isinstance(record.msg, LogEvent) or _is_project_logger(record.name):
        return record.getMessage()
    template = str(record.msg)
    safe_args: tuple[object, ...] | dict[str, object]
    try:
        if isinstance(args, Mapping) and "%(" in template:
            safe_args = {str(key): _safe_value(value, str(key)) for key, value in args.items()}
        elif isinstance(args, tuple):
            safe_args = tuple(_safe_value(value) for value in args)
        else:
            # 只给了一个映射参数、模板里又是位置占位符时，整个映射是一个参数。
            safe_args = (_safe_value(args),)
        return template % safe_args
    except Exception:
        return f"{template} {_ARGUMENTS_MISMATCH}"


class _LogFormatter(logging.Formatter):
    """在 record 副本上渲染单行消息（第三方库的参数先脱敏），异常回溯照常附在后面；控制台可选颜色。"""

    COLORS: ClassVar[dict[str, str]] = {
        "DEBUG"   : "\033[36m",
        "INFO"    : "\033[32m",
        "WARNING" : "\033[33m",
        "ERROR"   : "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, *, use_color: bool = False) -> None:
        super().__init__(fmt, _DATE_FORMAT)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        safe_record      = copy.copy(record)
        safe_record.msg  = _single_line(_record_message(record))
        safe_record.args = None
        # 别的 handler 可能已用标准格式把原始回溯缓存在 record 上，这里一律重新渲染。
        safe_record.exc_text = None
        if self._use_color and record.levelname in self.COLORS:
            safe_record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
        return super().format(safe_record)

    def formatException(self, ei: Any) -> str:  # noqa: N802 - logging.Formatter 的接口名
        exc = ei[1]
        if isinstance(exc, BaseException):
            return _render_exception(exc)
        return getattr(ei[0], "__name__", "Exception")


class _OutputFailures:
    """一个出口写不出日志时的诊断，代替标准库 ``Handler.handleError`` 的输出。

    标准库会把回溯（日志多半在 ``except`` 里写，业务异常的消息跟着异常链出来）和
    ``record.msg``、``record.args`` 原样印到 stderr。这里在 stderr 写一行固定格式的
    ``log.output_failed``：出口 ``handler``、阶段 ``stage`` 和失败的异常摘要，不写异常消息和
    记录内容，自身也不抛异常。同一阶段持续失败时第一次立即写，之后每分钟最多一行，带上期间
    压下的次数 ``suppressed``。
    """

    def __init__(self, handler: str) -> None:
        self._handler = handler
        # 阶段 → (上次写出诊断的 monotonic 时刻, 之后压下的次数)
        self._reported: dict[str, tuple[float, int]] = {}

    def report(self, stage: str, error: BaseException | None) -> None:
        try:
            now      = time.monotonic()
            reported = self._reported.get(stage)
            if reported is not None and now - reported[0] < _OUTPUT_FAILURE_INTERVAL_S:
                self._reported[stage] = (reported[0], reported[1] + 1)
                return
            self._reported[stage] = (now, 0)
            stream                = sys.stderr
            if stream is None:
                return
            # 异常链上是写日志时正在处理的业务异常，不是出口失败的原因，所以不写 cause。
            summary = None if error is None else summarize_exception(error)._replace(cause=None)
            event = LogEvent(
                "log.output_failed",
                handler    = self._handler,
                stage      = stage,
                error      = summary,
                suppressed = reported[1] if reported is not None and reported[1] else None,
            )
            stream.write(f"{time.strftime(_DATE_FORMAT)} [ERROR] [-] {__name__}: {event}\n")
            stream.flush()
        except Exception:
            pass


class _ConsoleHandler(logging.StreamHandler):
    """标准输出；写不出去（编码、写入、flush 失败）时报 ``handler=console stage=write``。"""

    def __init__(self, stream: TextIO) -> None:
        super().__init__(stream)
        self._failures = _OutputFailures("console")

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging 的接口名
        self._failures.report("write", sys.exc_info()[1])


class _FileHandler(TimedRotatingFileHandler):
    """``xiaoqing.log``，每天午夜轮转。

    轮转失败（例如 Windows 上文件被别的进程占着、改不了名）时报 ``stage=rollover``，这条
    照常写进当前文件，下一条再试轮转；写入失败报 ``stage=write``。
    """

    def __init__(self, filename: Path, *, keep_days: int) -> None:
        super().__init__(filename, when="midnight", backupCount=keep_days, encoding="utf-8")
        self._failures = _OutputFailures("file")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
        except Exception as exc:
            self._failures.report("rollover", exc)
        try:
            logging.FileHandler.emit(self, record)
        except Exception as exc:
            self._failures.report("write", exc)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging 的接口名
        self._failures.report("write", sys.exc_info()[1])


_configured_loggers: set[str] = set()


def _build_handlers(settings: LoggingSettings, log_dir: Path | None) -> list[logging.Handler]:
    """按配置建好出口；日志目录建不了、文件打不开时在这里抛出，还没动根 logger。"""

    handlers: list[logging.Handler] = []
    if settings.console:
        console = _ConsoleHandler(sys.stdout)
        console.setFormatter(_LogFormatter(_CONSOLE_FORMAT, use_color=_stdout_supports_color()))
        handlers.append(console)
    if settings.file and log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = _FileHandler(log_dir / _LOG_FILE_NAME, keep_days=settings.keep_days)
        file_handler.setFormatter(_LogFormatter(_FILE_FORMAT))
        handlers.append(file_handler)
    request_filter = _RequestIdFilter()
    for handler in handlers:
        handler.addFilter(request_filter)
    if not handlers:
        # 两个出口都关时明确丢弃，不让标准库的兜底 handler 把 WARNING 以上写到 stderr。
        handlers.append(logging.NullHandler())
    return handlers


def _install(handlers: list[logging.Handler], settings: LoggingSettings) -> None:
    """先挂上新出口再摘下、关闭旧的，根 logger 任何时刻都有出口；然后按配置设级别。"""

    root     = logging.getLogger()
    previous = list(root.handlers)
    for handler in handlers:
        root.addHandler(handler)
    for handler in previous:
        root.removeHandler(handler)
        for close_step in (handler.flush, handler.close):
            try:
                close_step()
            except Exception:
                pass

    level = logging.getLevelNamesMapping()[settings.level]
    root.setLevel(max(level, _OTHER_LOGGER_LEVEL))
    for name in _configured_loggers:
        logging.getLogger(name).setLevel(logging.NOTSET)
    named_levels = {**dict.fromkeys(_PROJECT_LOGGERS, settings.level), **settings.levels}
    for name, named_level in named_levels.items():
        logging.getLogger(name).setLevel(named_level)
    _configured_loggers.clear()
    _configured_loggers.update(named_levels)


def setup_logging(settings: LoggingSettings, log_dir: Path) -> None:
    """按配置接管根 logger：控制台和 ``log_dir/xiaoqing.log`` 两个出口，都带 request id。

    ``level`` 管项目自己的 logger（``core``、``plugins``、``scripts`` 和进程入口），其余
    logger 默认 WARNING、``level`` 更严时跟着更严；``levels`` 按名字单独设，两类都能调。
    新出口全部建好才换下旧的：日志目录建不了时异常抛给调用方，原来的出口还在。
    """

    _install(_build_handlers(settings, log_dir), settings)
    # 全仓库唯一不用 LogEvent 的日志：运维靠这句中文确认重启。
    logging.getLogger(__name__).info(
        "日志系统初始化完成 level=%s dir=%s keep_days=%d",
        settings.level,
        log_dir if settings.file else "-",
        settings.keep_days,
    )


# ============================================================
# 进程级的异常出口
# ============================================================


def bootstrap_logging() -> None:
    """进程入口在读配置之前调用：只开控制台、用默认级别，并接管未捕获异常的出口。

    读到配置后由 :func:`setup_logging` 换成配置的出口；在这之前，配置读不了、校验失败的
    诊断也按同样的格式和字段规则写出。解释器默认的 ``sys.excepthook``、
    ``threading.excepthook``、``sys.unraisablehook`` 把异常消息原样印到 stderr，这里换成
    记一条事件加安全回溯。
    """

    defaults = LoggingSettings(file=False)
    _install(_build_handlers(defaults, None), defaults)
    sys.excepthook       = _log_uncaught_exception
    threading.excepthook = _log_uncaught_thread_exception
    sys.unraisablehook   = _log_unraisable_exception


def _plain_name(name: object) -> str | None:
    """任务名、线程名是简单标识符时原样返回，否则不记。"""

    return name if isinstance(name, str) and _PLAIN_NAME_RE.fullmatch(name) else None


def _log_uncaught_exception(
    exc_type: type[BaseException], exc: BaseException, traceback: TracebackType | None
) -> None:
    logging.getLogger(__name__).critical(LogEvent("process.uncaught", error=exc), exc_info=exc)


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    if issubclass(args.exc_type, SystemExit):
        # 和标准库一样：线程里的 SystemExit 不算错误。
        return
    thread = args.thread
    logging.getLogger(__name__).error(
        LogEvent(
            "thread.uncaught",
            thread = _plain_name(thread.name if thread is not None else None),
            error  = args.exc_value,
        ),
        exc_info=args.exc_value,
    )


def _log_unraisable_exception(unraisable: sys.UnraisableHookArgs) -> None:
    logging.getLogger(__name__).error(
        LogEvent("process.unraisable", error=unraisable.exc_value),
        exc_info=unraisable.exc_value,
    )


def _asyncio_kind(
    context: Mapping[str, Any], owner: object, exception: BaseException | None
) -> str:
    if isinstance(owner, asyncio.Task):
        return "task_exception" if exception is not None else "pending_task_destroyed"
    if isinstance(owner, asyncio.Future) and exception is not None:
        return "future_exception"
    if "handle" in context:
        return "callback_exception"
    message = context.get("message")
    if isinstance(message, str) and message.casefold().startswith("unclosed"):
        return "unclosed_resource"
    return "other"


def asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """事件循环的异常处理器：记一条 ``asyncio.unhandled``，带种类码、任务名和异常摘要。

    asyncio 自带的处理器把 ``repr(task)``、``repr(exception)`` 拼进消息，异常消息就跟着落盘；
    这里不写 ``context["message"]``，种类码由 context 里的对象定：``task_exception``（任务
    异常没人取）、``future_exception``、``callback_exception``、``pending_task_destroyed``、
    ``unclosed_resource``（aiohttp 等报的未关闭资源）、``other``。有异常时附安全回溯。
    """

    exception = context.get("exception")
    if not isinstance(exception, BaseException):
        exception = None
    owner = context.get("future", context.get("task"))
    logging.getLogger("asyncio").error(
        LogEvent(
            "asyncio.unhandled",
            kind  = _asyncio_kind(context, owner, exception),
            task  = _plain_name(owner.get_name()) if isinstance(owner, asyncio.Task) else None,
            error = exception,
        ),
        exc_info=exception,
    )


__all__ = [
    "ExceptionSummary",
    "LogEvent",
    "LoggingSettings",
    "asyncio_exception_handler",
    "bootstrap_logging",
    "current_request_id",
    "exception_chain",
    "log_scope",
    "new_request_id",
    "setup_logging",
    "summarize_exception",
]
