"""公开错误：回复格式、按异常链分类、日志记录，以及异常消息哪里都不出现。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
import requests

import tests.helpers.app_test_support as _fixture_support
from core.ai import AIConfigError, AIRequestError
from core.bounded_http import HttpStatusError, ResponseFormatError, ResponseTransportError
from core.delivery import CommandOutcome
from core.log import LogEvent, log_scope
from core.public_errors import (
    PUBLIC_ERROR_PREFIX,
    log_public_error,
    public_error_message,
    public_error_response,
)
from core.safe_http import SafeHttpError
from tests.helpers.app_test_support import LoggedLines
from tests.helpers.assertions import logged_records, public_error_reply

restore_logging = _fixture_support.restore_logging
logged_lines    = _fixture_support.logged_lines

_THIS_FILE = "tests/core/test_public_errors.py"


@pytest.fixture(autouse=True)
def _capture_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)


def _logged_error(caplog: pytest.LogCaptureFixture) -> tuple[logging.LogRecord, LogEvent]:
    """恰好一条 ERROR 级的 ``public_error`` 记录，连同它的事件。"""

    (record,) = logged_records(caplog)
    assert record.msg.name == "public_error"
    assert record.levelno == logging.ERROR
    return record, record.msg


def _raised(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


def _raised_from(outer: BaseException, inner: BaseException) -> BaseException:
    """``outer`` 带着 ``inner`` 作为 ``__cause__`` 抛出。"""

    try:
        try:
            raise inner
        except BaseException as caught_inner:
            raise outer from caught_inner
    except BaseException as caught:
        return caught


def _raised_during(outer: BaseException, inner: BaseException) -> BaseException:
    """处理 ``inner`` 时又抛出 ``outer``：``inner`` 是 ``__context__``。"""

    try:
        try:
            raise inner
        except BaseException:
            raise outer  # noqa: B904 - 要的就是隐式 __context__
    except BaseException as caught:
        return caught


def _raise_site(exc: BaseException) -> str:
    """异常在本文件里抛出的那一行。"""

    traceback = exc.__traceback__
    assert traceback is not None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    return f"{_THIS_FILE}:{traceback.tb_lineno}"


def _sqlite_error(tmp_path: Path, *, busy: bool) -> sqlite3.Error:
    """真实的 SQLite 错误：语法错误，或另一连接持有排他锁时写入。"""

    path   = tmp_path / ("busy.sqlite" if busy else "syntax.sqlite")
    holder = sqlite3.connect(path)
    try:
        holder.execute("CREATE TABLE sample (value INTEGER)")
        holder.commit()
        if busy:
            holder.execute("BEGIN EXCLUSIVE")
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("INSERT INTO sample VALUES (1)" if busy else "SELEC")
        except sqlite3.Error as exc:
            return exc
        finally:
            other.close()
    finally:
        holder.close()
    raise AssertionError("SQLite did not fail")


# (类别, 造异常；异常类型名和位置都取它本身)
_CATEGORY_CASES: list[tuple[str, Callable[[Path], BaseException]]] = [
    ("timeout", lambda _tmp: TimeoutError()),
    ("timeout", lambda _tmp: requests.ReadTimeout()),
    ("timeout", lambda _tmp: requests.ConnectTimeout()),
    ("timeout", lambda _tmp: aiohttp.ServerTimeoutError()),
    ("network", lambda _tmp: ConnectionResetError()),
    ("network", lambda _tmp: aiohttp.ServerDisconnectedError()),
    ("network", lambda _tmp: requests.ConnectionError()),
    ("network", lambda _tmp: ResponseTransportError("body stream failed")),
    ("upstream_status", lambda _tmp: HttpStatusError(503)),
    ("upstream_status", lambda _tmp: aiohttp.ClientResponseError(None, (), status=404)),
    (
        "upstream_status",
        lambda _tmp: requests.HTTPError(response=SimpleNamespace(status_code=502)),
    ),
    ("bad_response", lambda _tmp: aiohttp.ContentTypeError(None, ())),
    ("bad_response", lambda _tmp: json.JSONDecodeError("bad", "{", 0)),
    ("bad_response", lambda _tmp: ResponseFormatError("bad content type")),
    ("bad_response", lambda _tmp: SafeHttpError("response is too large")),
    ("model", lambda _tmp: AIRequestError("rate_limit", status=429)),
    ("model", lambda _tmp: AIConfigError("route missing")),
    ("dependency", lambda _tmp: ModuleNotFoundError("missing", name="optional_dep")),
    ("database", lambda tmp: _sqlite_error(tmp, busy=False)),
    ("io", lambda _tmp: PermissionError()),
    ("io", lambda _tmp: FileNotFoundError()),
    ("internal", lambda _tmp: KeyError("k")),
    ("internal", lambda _tmp: TypeError()),
    ("internal", lambda _tmp: AttributeError()),
    ("internal", lambda _tmp: ValueError()),
    ("internal", lambda _tmp: AssertionError()),
    ("internal", lambda _tmp: RuntimeError()),
]


@pytest.mark.parametrize(
    ("category", "build"),
    _CATEGORY_CASES,
    ids=[f"{category}-{index}" for index, (category, _build) in enumerate(_CATEGORY_CASES)],
)
def test_reply_names_category_type_location_and_request_id(
    category: str,
    build: Callable[[Path], BaseException],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    built = build(tmp_path)
    error = built if built.__traceback__ is not None else _raised(built)

    message = public_error_message(
        SimpleNamespace(request_id="req-category"), error, component="demo.handle"
    )

    reply = public_error_reply(message)
    assert message == reply.group(0)
    assert reply.group("type_name") == type(error).__name__
    assert reply.group("location") == _raise_site(error)
    assert reply.group("request_id") == "req-category"
    record, logged = _logged_error(caplog)
    assert record.request_id == "req-category"
    assert logged.fields["component"] == "demo.handle"
    assert logged.fields["category"] == category
    assert logged.fields["error"] == type(error).__name__
    assert logged.fields["error.at"] == _raise_site(error)


def test_each_category_has_its_own_reason_and_carries_status_and_module(
    tmp_path: Path,
) -> None:
    def reason_of(exc: BaseException) -> str:
        message = public_error_message(None, _raised(exc), component="demo")
        return public_error_reply(message)["reason"]

    # 每类取表里的第一个例子。
    first_of_each = dict(reversed(_CATEGORY_CASES))
    reasons = {category: reason_of(build(tmp_path)) for category, build in first_of_each.items()}

    assert len(set(reasons.values())) == len(reasons)
    assert "503" in reason_of(HttpStatusError(503))
    assert "404" in reason_of(HttpStatusError(404))
    assert "optional_dep" in reason_of(ModuleNotFoundError("missing", name="optional_dep"))
    assert reason_of(_sqlite_error(tmp_path, busy=True)) != reasons["database"]
    assert reason_of(AIRequestError("authentication", status=401)) != reason_of(
        AIRequestError("rate_limit", status=429)
    )
    assert reason_of(AIConfigError("route missing")) != reason_of(AIRequestError("timeout"))


def test_model_request_errors_log_their_category_as_reason_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    public_error_message(None, _raised(AIRequestError("rate_limit", status=429)), component="demo")

    _, logged = _logged_error(caplog)
    assert logged.fields["category"] == "model"
    assert logged.fields["reason_code"] == "rate_limit"
    assert logged.fields["error.status"] == 429


def test_first_categorised_link_outermost_first_decides_type_and_location(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeout_cause = _raised_from(RuntimeError("wrapper"), TimeoutError())
    inner_timeout = timeout_cause.__cause__
    assert inner_timeout is not None
    during_network = _raised_during(KeyError("handler bug"), ConnectionResetError())
    inner_network  = during_network.__context__
    assert inner_network is not None
    uncategorised = _raised_from(RuntimeError("outer"), KeyError("inner"))

    by_cause = public_error_reply(public_error_message(None, timeout_cause, component="a"))
    by_context = public_error_reply(public_error_message(None, during_network, component="b"))
    internal = public_error_reply(public_error_message(None, uncategorised, component="c"))

    assert (by_cause["type_name"], by_cause["location"]) == (
        "TimeoutError",
        _raise_site(inner_timeout),
    )
    assert (by_context["type_name"], by_context["location"]) == (
        "ConnectionResetError",
        _raise_site(inner_network),
    )
    assert (internal["type_name"], internal["location"]) == (
        "RuntimeError",
        _raise_site(uncategorised),
    )
    events = [record.msg for record in logged_records(caplog)]
    assert [event.fields["category"] for event in events] == ["timeout", "network", "internal"]
    # 日志的 error 字段总是最外层的异常，最里层的原因记在 error.cause。
    assert events[0].fields["error"] == "RuntimeError"
    assert events[0].fields["error.cause"] == "TimeoutError"


def test_wrapper_errors_defer_to_a_categorised_cause(caplog: pytest.LogCaptureFixture) -> None:
    wrapped_timeout = _raised_from(
        SafeHttpError("body failed"),
        _raised_from(ResponseTransportError("stream failed"), TimeoutError()),
    )
    wrapped_unknown = _raised_from(SafeHttpError("body failed"), KeyError("x"))

    deferred = public_error_reply(public_error_message(None, wrapped_timeout, component="a"))
    own = public_error_reply(public_error_message(None, wrapped_unknown, component="b"))

    assert deferred["type_name"] == "TimeoutError"
    assert own["type_name"] == "SafeHttpError"
    assert [record.msg.fields["category"] for record in logged_records(caplog)] == [
        "timeout",
        "bad_response",
    ]


def test_location_falls_back_to_the_outer_exception_or_is_omitted() -> None:
    unraised_cause           = _raised(RuntimeError("outer"))
    unraised_cause.__cause__ = TimeoutError()

    with_fallback = public_error_reply(public_error_message(None, unraised_cause, component="a"))
    never_raised = public_error_reply(public_error_message(None, KeyError("k"), component="b"))

    assert with_fallback["type_name"] == "TimeoutError"
    assert with_fallback["location"] == _raise_site(unraised_cause)
    assert never_raised["type_name"] == "KeyError"
    assert never_raised["location"] is None


def test_response_is_one_error_segment_and_logs_once_with_exc_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = _raised(ValueError("private"))

    response = public_error_response(
        SimpleNamespace(request_id="req-200"), error, component="demo.response"
    )

    assert response.command_outcome == CommandOutcome("error")
    (segment,) = response
    assert segment["type"] == "text"
    assert public_error_reply(segment["data"]["text"])["request_id"] == "req-200"
    record, _ = _logged_error(caplog)
    assert record.exc_info is not None and record.exc_info[1] is error


def test_log_public_error_writes_the_same_record_without_a_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log_public_error(
        SimpleNamespace(request_id="req-log-only", plugin_name="demo"),
        _raised(ConnectionResetError()),
        component="demo.bg",
    )

    record, logged = _logged_error(caplog)
    assert record.name == "plugins.demo"
    assert record.request_id == "req-log-only"
    assert logged.fields["category"] == "network"
    assert logged.fields["component"] == "demo.bg"


def test_public_error_is_logged_under_the_plugin_namespace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    for plugin_name in ("demo", "bad name\n", None):
        public_error_message(
            SimpleNamespace(plugin_name=plugin_name, request_id="req-1"),
            _raised(RuntimeError("failure")),
            component="demo.handle",
        )

    assert [record.name for record in caplog.records] == [
        "plugins.demo",
        "core.public_errors",
        "core.public_errors",
    ]


def test_request_id_is_the_contexts_else_the_log_scopes_else_a_new_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = _raised(RuntimeError("failure"))

    from_context = public_error_message(
        SimpleNamespace(request_id="abcd1234.command.op-1"), error, component="a"
    )
    with log_scope("feedbeef"):
        malformed = public_error_message(
            SimpleNamespace(request_id="bad id\nAuthorization: x"), error, component="b"
        )
        missing = public_error_message(None, error, component="c")
    fresh = public_error_message(SimpleNamespace(request_id=None), error, component="d")

    reply_ids = [
        public_error_reply(text)["request_id"] for text in (from_context, malformed, missing, fresh)
    ]
    assert reply_ids[:3] == ["abcd1234.command.op-1", "feedbeef", "feedbeef"]
    assert re.fullmatch(r"[0-9a-f]{8}", reply_ids[3])
    assert [record.request_id for record in caplog.records] == reply_ids


def test_logging_failure_cannot_replace_the_reply() -> None:
    class BrokenSink(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            raise OSError("broken log sink")

    sink   = BrokenSink()
    target = logging.getLogger("core.public_errors")
    target.addFilter(sink)
    try:
        message = public_error_message(
            SimpleNamespace(request_id="req-log-failure"),
            _raised(RuntimeError("private")),
            component="demo.logger",
        )
    finally:
        target.removeFilter(sink)

    assert public_error_reply(message)["request_id"] == "req-log-failure"


# 异常消息里的探针：用户原话、各种凭据写法、URL、路径。
_CANARIES = (
    "用户的悄悄话-CANARY-PLAIN",
    "token=CANARY-TOKEN",
    "Cookie: session=CANARY-SESSION",
    '{"api_key": "CANARY-API-KEY"}',
    "https://user:pw@example.test/private?q=CANARY-URL",
    r"C:\Users\alice\CANARY-WIN-PATH.txt",
    "/home/alice/CANARY-POSIX-PATH",
)


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise ValueError("CANARY-STR-RAISES")

    def __repr__(self) -> str:
        raise ValueError("CANARY-REPR-RAISES")


def _nested_chain() -> BaseException:
    try:
        try:
            try:
                raise OSError(" ".join(_CANARIES[4:]))
            except OSError as innermost:
                raise ValueError(" ".join(_CANARIES[2:4])) from innermost
        except ValueError:
            raise RuntimeError(" ".join(_CANARIES[:2]))  # noqa: B904 - 隐式 __context__
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    ("build", "category", "type_name"),
    [
        pytest.param(_nested_chain, "io", "OSError", id="nested-cause-and-context"),
        pytest.param(
            lambda: _raised(RuntimeError("x" * 40_000 + " ".join(_CANARIES))),
            "internal",
            "RuntimeError",
            id="oversized",
        ),
        pytest.param(
            lambda: _raised(_UnprintableError()), "internal", "_UnprintableError", id="unprintable"
        ),
    ],
)
def test_exception_messages_never_reach_the_reply_or_the_log_file(
    build: Callable[[], BaseException],
    category: str,
    type_name: str,
    logged_lines: LoggedLines,
) -> None:
    message = public_error_message(
        SimpleNamespace(request_id="req-canary", plugin_name="demo"), build(), component="demo.x"
    )

    log_text = "\n".join(logged_lines.lines())
    for text in (message, log_text):
        for canary in (*_CANARIES, "CANARY"):
            assert canary not in text
    assert message.startswith(PUBLIC_ERROR_PREFIX)
    reply = public_error_reply(message)
    assert reply["type_name"] == type_name
    line = logged_lines.line_with("public_error")
    assert line.group("level") == "ERROR"
    assert line.group("logger") == "plugins.demo"
    assert line.group("request_id") == "req-canary"
    assert f"category={category}" in line.group("message")
    # 回溯照常附在记录后面：出错位置在里面，异常消息不在。
    assert reply["location"] in log_text
