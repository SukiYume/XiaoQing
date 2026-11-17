"""日志系统：初始化与级别、进程级异常出口、request id 关联、输出格式、出口写不出去、事件脱敏、
配置校验，以及请求、定时任务和事件循环里的记录。"""

from __future__ import annotations

import asyncio
import datetime
import errno
import gc
import importlib
import inspect
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from enum import IntEnum
from fractions import Fraction
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
import pytest
from pydantic import ValidationError

import tests.helpers.app_test_support as _fixture_support
from core.delivery import DeliveryReceipt, attach_receipt
from core.log import (
    LogEvent,
    LoggingSettings,
    _enable_windows_ansi,
    bootstrap_logging,
    current_request_id,
    log_scope,
    setup_logging,
)
from core.onebot import OneBotWsClient
from tests.helpers.app_test_support import (
    PROBE,
    LoggedLines,
    XiaoQingApp,
    command_declaration,
    console_lines,
    event_fields,
    load_test_plugin,
)
from tests.helpers.assertions import redacted_field
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.server_test_support import (
    auth_headers,
    make_server,
    onebot_message_payload,
    post_event,
    serving,
    wait_until,
)

temp_app_root                               = _fixture_support.temp_app_root
restore_logging                             = _fixture_support.restore_logging
logged_lines                                = _fixture_support.logged_lines
_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

try:
    import numpy
except ImportError:  # numpy 是可选依赖
    numpy = None

log = logging.getLogger("plugins.demo")

_MASKED = re.compile(r"<\d+(?:ch|B) #[0-9a-f]{8}>")
_needs_numpy = pytest.mark.skipif(numpy is None, reason="numpy 未安装")


def _setup(tmp_path: Path, **settings: Any) -> LoggedLines:
    settings.setdefault("console", False)
    log_dir = tmp_path / "logs"
    setup_logging(LoggingSettings(**settings), log_dir)
    return LoggedLines(log_dir)


def _file_handlers() -> list[TimedRotatingFileHandler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]


def _module_from_source(source: str, filename: str) -> dict[str, Any]:
    """按给定文件名编译执行一段代码：回溯里的帧就落在这个文件名上（不读源码，文件不必存在）。"""

    namespace: dict[str, Any] = {}
    exec(compile(source, filename, "exec"), namespace)
    return namespace


def _raised(fail: Callable[[], object]) -> BaseException:
    try:
        fail()
    except BaseException as exc:
        return exc
    raise AssertionError("expected an exception")


class _Priority(IntEnum):
    HIGH = 3


def _raise_with(error: BaseException, **attributes: object) -> None:
    """给异常挂上属性再抛出，像 HTTP 客户端把状态码、响应挂在异常上那样。"""

    for name, value in attributes.items():
        setattr(error, name, value)
    raise error


# ============================================================
# setup_logging
# ============================================================


@pytest.mark.usefixtures("restore_logging")
class TestSetup:
    def test_only_one_log_file_is_written(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        log.error("boom")

        assert [path.name for path in logged.path.parent.iterdir()] == ["xiaoqing.log"]

    def test_file_rotates_daily_and_keeps_the_configured_number_of_copies(
        self, tmp_path: Path
    ) -> None:
        _setup(tmp_path, keep_days=7)

        (handler,) = _file_handlers()

        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == 7

    def test_console_and_file_switch_independently(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        console_only = _setup(tmp_path, console=True, file=False)
        log.info("console-only")
        assert "console-only" in capsys.readouterr().out
        assert not console_only.path.parent.exists()

        file_only = _setup(tmp_path, console=False, file=True)
        log.info("file-only")
        assert capsys.readouterr().out == ""
        assert "file-only" in "\n".join(file_only.lines())

        _setup(tmp_path / "off", console=False, file=False)
        log.error("nowhere")
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")

    def test_initialisation_line_confirms_the_settings(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path, keep_days=3)

        first = logged.lines()[0]

        assert "core.log" in first
        assert f"日志系统初始化完成 level=INFO dir={logged.path.parent} keep_days=3" in first

    def test_a_new_setup_closes_and_replaces_the_previous_handlers(self, tmp_path: Path) -> None:
        _setup(tmp_path / "first")
        (previous,) = _file_handlers()

        _setup(tmp_path / "second")

        assert previous.stream is None
        assert previous not in logging.getLogger().handlers
        assert len(_file_handlers()) == 1

    def test_level_covers_the_project_loggers_and_other_loggers_stay_at_warning(
        self, tmp_path: Path
    ) -> None:
        logged = _setup(tmp_path, level="DEBUG")
        project = ("core.app", "plugins.demo", "scripts.chat_eval", "main", "__main__")

        for name in project:
            logging.getLogger(name).debug(f"debug-from-{name}")
        # 第三方库在 DEBUG、INFO 写请求头、原始帧和带查询串的 URL，这些记录不经过字段脱敏。
        logging.getLogger("websockets.client").debug("> Authorization: Bearer WS-CANARY")
        logging.getLogger("urllib3.connectionpool").info("GET /x?token=URL-CANARY")
        logging.getLogger("websockets.client").warning("ws-warning")

        text = "\n".join(logged.lines())
        assert all(f"debug-from-{name}" in text for name in project)
        assert "WS-CANARY" not in text and "URL-CANARY" not in text
        assert "ws-warning" in text

    def test_levels_lower_or_raise_single_loggers_and_a_stricter_level_applies_to_all(
        self, tmp_path: Path
    ) -> None:
        logged = _setup(
            tmp_path,
            level  = "WARNING",
            levels = {"plugins.demo": "DEBUG", "websockets": "DEBUG", "core": "ERROR"},
        )

        logging.getLogger("plugins.demo").debug("demo-debug")
        logging.getLogger("websockets.client").debug("ws-debug")
        logging.getLogger("core.app").warning("core-warning")
        logging.getLogger("other.noisy").info("other-info")
        logging.getLogger("other.noisy").warning("other-warning")

        text = "\n".join(logged.lines())
        assert "demo-debug" in text and "ws-debug" in text and "other-warning" in text
        assert "core-warning" not in text and "other-info" not in text

        strict = _setup(tmp_path / "strict", level="ERROR")
        logging.getLogger("other.noisy").warning("strict-other-warning")
        logging.getLogger("plugins.demo").warning("strict-demo-warning")
        logging.getLogger("other.noisy").error("strict-other-error")

        text = "\n".join(strict.lines())
        assert "strict-other-error" in text
        assert "strict-other-warning" not in text and "strict-demo-warning" not in text

    def test_a_new_setup_forgets_levels_from_the_previous_one(self, tmp_path: Path) -> None:
        _setup(tmp_path, levels={"plugins.demo": "DEBUG", "websockets": "DEBUG"})

        logged = _setup(tmp_path / "again")
        log.debug("forgotten-debug")
        logging.getLogger("websockets.client").info("forgotten-ws-info")

        text = "\n".join(logged.lines())
        assert "forgotten-debug" not in text and "forgotten-ws-info" not in text

    def test_bootstrap_writes_to_the_console_until_the_configured_setup_replaces_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bootstrap_logging()
        log.info("before-config")
        logging.getLogger("websockets.client").info("bootstrap-ws-info")

        assert [line["message"] for line in console_lines(capsys.readouterr().out)] == [
            "before-config"
        ]

        logged = _setup(tmp_path)
        log.info("after-config")

        assert capsys.readouterr().out == ""
        assert logged.line_with("after-config")["level"] == "INFO"

    def test_a_log_directory_that_cannot_be_created_leaves_the_previous_outputs_in_place(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bootstrap_logging()
        (tmp_path / "logs").write_text("", encoding="utf-8")  # 同名文件占着，目录建不了

        with pytest.raises(OSError):
            setup_logging(LoggingSettings(console=False), tmp_path / "logs")
        log.error("still-on-console")

        assert [line["message"] for line in console_lines(capsys.readouterr().out)] == [
            "still-on-console"
        ]

    def test_bootstrap_logs_uncaught_exceptions_as_events_without_their_messages(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        canary = "Authorization: Bearer UNCAUGHT-CANARY"
        bootstrap_logging()

        try:
            raise ValueError(canary)
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)

        def fail_in_thread() -> None:
            raise KeyError(canary)

        worker = threading.Thread(target=fail_in_thread, name="worker-1")
        worker.start()
        worker.join()

        class Leaky:
            def __del__(self) -> None:
                raise OSError(canary)

        Leaky()
        gc.collect()

        output = capsys.readouterr()
        events = dict(event_fields(line["message"]) for line in console_lines(output.out))
        assert events["process.uncaught"]["error"] == "ValueError"
        assert (events["thread.uncaught"]["thread"], events["thread.uncaught"]["error"]) == (
            "worker-1",
            "KeyError",
        )
        assert events["process.unraisable"]["error"] == "OSError"
        assert "UNCAUGHT-CANARY" not in output.out + output.err
        # 安全回溯照常附上：调用位置在这个测试文件里。
        assert "tests/core/test_logging.py:" in output.out

    def test_console_has_colour_only_on_a_tty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _setup(tmp_path, console=True, file=False)
        log.warning("plain")
        assert "\033[" not in capsys.readouterr().out

        class _Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        tty = _Tty()
        monkeypatch.setattr(sys, "stdout", tty)
        monkeypatch.setattr(sys, "platform", "linux")
        _setup(tmp_path, console=True, file=True)
        log.warning("coloured")

        assert "\033[33mWARNING\033[0m" in tty.getvalue()
        assert "\033[" not in "\n".join(LoggedLines(tmp_path / "logs").lines())

    def test_windows_ansi_preserves_existing_console_mode_bits(self) -> None:
        kernel32 = _FakeKernel32(mode=0x23)

        assert _enable_windows_ansi(kernel32) is True
        assert kernel32.set_calls == [(123, 0x27)]

    def test_windows_ansi_fails_closed_when_mode_cannot_be_read(self) -> None:
        kernel32 = _FakeKernel32(get_mode_ok=False)

        assert _enable_windows_ansi(kernel32) is False
        assert kernel32.set_calls == []


class _FakeKernel32:
    def __init__(self, *, mode: int = 0x20, get_mode_ok: bool = True) -> None:
        self.mode                             = mode
        self.get_mode_ok                      = get_mode_ok
        self.set_calls: list[tuple[int, int]] = []

    def GetStdHandle(self, identifier: int) -> int:
        assert identifier == -11
        return 123

    def GetConsoleMode(self, handle: int, mode_pointer: Any) -> int:
        assert handle == 123
        if not self.get_mode_ok:
            return 0
        mode_pointer._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        self.set_calls.append((handle, mode))
        return 1


# ============================================================
# request id：scope 与 filter
# ============================================================


@pytest.mark.usefixtures("restore_logging")
class TestRequestId:
    def test_lines_carry_the_scope_id_inside_and_a_dash_outside(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        log.info("before")
        with log_scope("req-1"):
            assert current_request_id() == "req-1"
            log.info("inside")
        log.info("after")

        assert current_request_id() is None
        assert logged.request_id_of("before") == "-"
        assert logged.request_id_of("inside") == "req-1"
        assert logged.request_id_of("after") == "-"

    def test_nested_scopes_restore_the_outer_id_on_exit_even_after_an_error(
        self, tmp_path: Path
    ) -> None:
        logged = _setup(tmp_path)

        with log_scope("outer"):
            with pytest.raises(RuntimeError):
                with log_scope("inner"):
                    log.info("in-inner")
                    raise RuntimeError("boom")
            log.info("back-in-outer")

        assert logged.request_id_of("in-inner") == "inner"
        assert logged.request_id_of("back-in-outer") == "outer"
        assert current_request_id() is None

    def test_an_explicit_request_id_wins_over_the_scope(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        with log_scope("scoped"):
            log.info("explicit", extra={"request_id": "explicit-id"})
            log.info("implicit")

        assert logged.request_id_of("explicit") == "explicit-id"
        assert logged.request_id_of("implicit") == "scoped"

    @pytest.mark.asyncio
    async def test_tasks_and_threads_started_in_a_scope_keep_its_id(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        async def work() -> None:
            log.info("from-task")

        with log_scope("req-9"):
            await asyncio.create_task(work())
            await asyncio.to_thread(log.info, "from-thread")

        assert logged.request_id_of("from-task") == "req-9"
        assert logged.request_id_of("from-thread") == "req-9"

    def test_a_none_scope_clears_the_id_for_its_block(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        with log_scope("outer"):
            with log_scope(None):
                assert current_request_id() is None
                log.info("cleared")
            log.info("restored")

        assert logged.request_id_of("cleared") == "-"
        assert logged.request_id_of("restored") == "outer"


# ============================================================
# 输出格式
# ============================================================


@pytest.mark.usefixtures("restore_logging")
class TestFormat:
    def test_line_breaks_and_control_characters_cannot_forge_log_lines(
        self, tmp_path: Path
    ) -> None:
        logged = _setup(tmp_path)

        log.info("a\nb\r\x00c\x7f\x1bd\tz")

        message = logged.line_with("b").group("message")
        assert message == "a\\nb\\r\\x00c\\x7f\\x1bd\tz"
        assert len(logged.lines()) == 2  # 初始化那一行 + 这一行

    def test_overlong_messages_are_truncated_with_the_dropped_count(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        log.info("x" * 5000)

        assert logged.line_with("xxxx").group("message") == "x" * 4000 + "…(+1000 chars)"

    def test_tracebacks_keep_types_and_call_sites_but_never_exception_messages(
        self, tmp_path: Path
    ) -> None:
        logged = _setup(tmp_path)
        canary = "Authorization: Bearer TRACEBACK-CANARY"

        def fail() -> None:
            raise KeyError(canary)

        try:
            try:
                fail()
            except KeyError as exc:
                error        = ValueError(canary + "\n" + "x" * 5000)
                error.status = 504  # type: ignore[attr-defined]
                raise error from exc
        except ValueError:
            log.exception("failed")

        text = "\n".join(logged.lines())
        assert logged.line_with("failed").group("message") == "failed"
        assert "tests/core/test_logging.py:" in text and " in fail" in text
        assert "KeyError" in text and "direct cause" in text
        assert "\nValueError (status=504)" in text  # 类型行带上状态码一类的属性
        assert "TRACEBACK-CANARY" not in text and "x" * 100 not in text
        assert str(Path.home()) not in text[text.index("Traceback") :]  # 回溯里只有相对路径

        # 别的 handler 先用标准格式缓存在记录上的原始回溯，也不会被带进日志。
        (handler,) = _file_handlers()
        try:
            raise ValueError(canary)
        except ValueError as exc:
            record = logging.LogRecord(
                "t", logging.ERROR, __file__, 1, "cached", None, (ValueError, exc, None)
            )
        record.request_id = "-"
        logging.Formatter().format(record)
        assert "TRACEBACK-CANARY" in (record.exc_text or "")
        assert "TRACEBACK-CANARY" not in handler.format(record)

    def test_a_deep_traceback_keeps_the_innermost_frames(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        def recurse(depth: int) -> None:
            if depth:
                recurse(depth - 1)
            raise KeyError("DEEP-CANARY")

        try:
            recurse(80)
        except KeyError:
            log.exception("deep")

        frames = [line for line in logged.lines() if line.startswith("  ") and " in " in line]
        assert len(frames) == 48
        assert all(frame.endswith(" in recurse") for frame in frames)

    def test_file_lines_name_the_calling_file_and_line_but_console_lines_do_not(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        logged = _setup(tmp_path, console=True)
        frame = inspect.currentframe()
        assert frame is not None
        call_line = frame.f_lineno + 2

        log.info("where-am-i")

        expected = f"{Path(__file__).name}:{call_line}"
        assert logged.line_with("where-am-i").group("where") == expected
        console_line = next(
            line for line in capsys.readouterr().out.splitlines() if "where-am-i" in line
        )
        assert Path(__file__).name not in console_line
        assert re.match(r"^\S+ \S+ \[INFO\] \[-\] plugins\.demo: where-am-i$", console_line)

    def test_formatting_does_not_change_the_record_other_handlers_receive(
        self, tmp_path: Path
    ) -> None:
        received: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                received.append(record)

        logged = _setup(tmp_path)
        logging.getLogger().addHandler(_Collect())

        log.info("a\nb")

        assert received[0].getMessage() == "a\nb"
        assert "a\\nb" in logged.line_with("a").group("message")

    def test_library_records_keep_their_template_and_arguments_follow_the_field_rules(
        self, tmp_path: Path
    ) -> None:
        logged = _setup(tmp_path, levels={"websockets": "DEBUG", "urllib3": "DEBUG"})

        class Peer:
            def __str__(self) -> str:
                return "peer OBJECT-CANARY"

        # websockets 记请求头、urllib3 记带查询串的请求行时的调用形状。
        logging.getLogger("websockets.client").debug(
            "> %s: %s", "Authorization", "Bearer WS-CANARY"
        )
        # 不带空格的头部值长得和标识符一样，也可能就是凭据。
        logging.getLogger("websockets.client").debug("> %s: %s", "X-Api-Key", "BARE-TOKEN-CANARY")
        logging.getLogger("urllib3.connectionpool").debug(
            '%s://%s:%s "%s %s %s" %s %s',
            "https",
            "api.example.com",
            443,
            "GET",
            "/v1/items?token=URL-CANARY",
            "HTTP/1.1",
            200,
            512,
        )
        logging.getLogger("aiohttp.server").warning("Error handling request from %s", Peer())
        logging.getLogger("aiohttp.web").warning("slow: %.2f s for %d requests", 1.5, 3)
        logging.getLogger("aiohttp.web").warning(
            "%(peer)s sent %(count)d frames, %(token)s",
            {"peer": "127.0.0.1", "count": 4, "token": "t"},
        )
        logging.getLogger("aiohttp.web").warning("headers %s", {"Authorization": "DICT-CANARY"})
        logging.getLogger("aiohttp.web").warning("%d frames from FORMAT", "FORMAT-CANARY")
        log.warning("project %s", "two words")  # 项目自己的 logger 照常格式化

        text = "\n".join(logged.lines())
        for canary in (
            "WS-CANARY",
            "BARE-TOKEN-CANARY",
            "URL-CANARY",
            "OBJECT-CANARY",
            "DICT-CANARY",
            "FORMAT-CANARY",
        ):
            assert canary not in text
        assert logged.line_with(redacted_field("Bearer WS-CANARY"))["message"] == (
            f"> {redacted_field('Authorization')}: {redacted_field('Bearer WS-CANARY')}"
        )
        assert logged.line_with(redacted_field("api.example.com"))["message"] == (
            f"{redacted_field('https')}://{redacted_field('api.example.com')}:443 "
            f'"{redacted_field("GET")} {redacted_field("/v1/items?token=URL-CANARY")} '
            f'{redacted_field("HTTP/1.1")}" 200 512'
        )
        peer = logged.line_with("Error handling")["message"].removeprefix(
            "Error handling request from "
        )
        assert _MASKED.fullmatch(peer)
        assert logged.line_with("slow:")["message"] == "slow: 1.50 s for 3 requests"
        assert logged.line_with("sent 4 frames")["message"].startswith("127.0.0.1 sent 4 frames, ")
        assert logged.line_with("headers")["message"] == "headers [1 items]"
        assert logged.line_with("frames from FORMAT")["message"].startswith("%d frames from FORMAT")
        assert logged.line_with("project")["message"] == "project two words"


# ============================================================
# 出口写不出去
# ============================================================

_EXCEPTION_CANARY = "Authorization: Bearer HANDLER-EXCEPTION-CANARY"
_RECORD_CANARY    = "HANDLER-RECORD-CANARY"


class _FailingStream(io.StringIO):
    """控制台或日志文件背后的流：``failure`` 设上以后在 OS 或编码边界上按这种方式失败。"""

    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None

    def write(self, text: str) -> int:
        if self.failure == "encode":
            text.encode("ascii")  # 记录里有非 ASCII 字符时抛 UnicodeEncodeError
        elif self.failure == "write":
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(text)

    def flush(self) -> None:
        if self.failure == "flush":
            raise OSError(errno.EIO, "Input/output error")
        super().flush()


def _log_while_handling_an_error() -> None:
    """在处理业务异常的 except 里写日志：出口失败的异常链上就挂着这个带消息的业务异常。"""

    try:
        raise KeyError(_EXCEPTION_CANARY)
    except KeyError:
        log.exception(LogEvent("probe.failed", reply=_RECORD_CANARY))
        log.error("小青 %s", _RECORD_CANARY)


def _output_failures(stderr: str) -> list[dict[str, str]]:
    events = (event_fields(line["message"]) for line in console_lines(stderr))
    return [fields for name, fields in events if name == "log.output_failed"]


@pytest.mark.usefixtures("restore_logging")
class TestOutputFailures:
    @pytest.mark.parametrize(
        ("failure", "error"),
        [("write", "OSError"), ("flush", "OSError"), ("encode", "UnicodeEncodeError")],
    )
    def test_a_console_that_cannot_write_reports_one_line_without_messages_or_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        failure: str,
        error: str,
    ) -> None:
        stream = _FailingStream()
        monkeypatch.setattr(sys, "stdout", stream)
        _setup(tmp_path, console=True, file=False)
        stream.failure = failure

        _log_while_handling_an_error()

        stderr = capsys.readouterr().err
        (report,) = _output_failures(stderr)
        assert (report["handler"], report["stage"], report["error"]) == ("console", "write", error)
        assert "error.cause" not in report  # 异常链上的业务异常不是出口失败的原因
        assert "EXCEPTION-CANARY" not in stderr and _RECORD_CANARY not in stderr

    def test_a_log_file_that_cannot_be_written_reports_without_messages_or_records(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _setup(tmp_path)
        (handler,) = _file_handlers()
        stream   = _FailingStream()
        previous = handler.setStream(stream)
        assert previous is not None
        previous.close()
        stream.failure = "write"

        _log_while_handling_an_error()

        stderr = capsys.readouterr().err
        (report,) = _output_failures(stderr)
        assert (report["handler"], report["stage"], report["error"]) == ("file", "write", "OSError")
        assert report["error.errno"] == str(errno.ENOSPC)
        assert "EXCEPTION-CANARY" not in stderr and _RECORD_CANARY not in stderr

    def test_a_blocked_rollover_is_reported_and_the_record_still_lands_in_the_current_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logged       = _setup(tmp_path)
        day_after    = time.time() + 2 * 86400
        held_open_by = "C:/Users/alice/viewer.exe"

        def rename_while_held_open(source: str, destination: str) -> None:
            raise PermissionError(errno.EACCES, f"in use by {held_open_by}", source)

        monkeypatch.setattr(time, "time", lambda: day_after)
        monkeypatch.setattr(os, "rename", rename_while_held_open)

        _log_while_handling_an_error()

        stderr = capsys.readouterr().err
        (report,) = _output_failures(stderr)
        assert (report["handler"], report["stage"], report["error"]) == (
            "file",
            "rollover",
            "PermissionError",
        )
        assert "EXCEPTION-CANARY" not in stderr and _RECORD_CANARY not in stderr
        assert held_open_by not in stderr and str(tmp_path) not in stderr
        assert logged.line_with("probe.failed")["level"] == "ERROR"
        assert [path.name for path in logged.path.parent.iterdir()] == ["xiaoqing.log"]

    def test_a_persistent_failure_is_reported_once_a_minute_with_the_suppressed_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stream = _FailingStream()
        monkeypatch.setattr(sys, "stdout", stream)
        _setup(tmp_path, console=True, file=False)
        now = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        stream.failure = "write"

        for _ in range(3):
            log.error("lost")
        now[0] += 30
        log.error("lost")
        now[0] += 31
        log.error("lost")

        reports = _output_failures(capsys.readouterr().err)
        assert [report.get("suppressed") for report in reports] == [None, "3"]


# ============================================================
# LogEvent()
# ============================================================


class TestEventFields:
    def test_body_keys_are_masked_even_when_the_value_looks_like_a_token(self) -> None:
        item = LogEvent(
            "x",
            reason      = "private",
            message     = "ok",
            reply       = "hello",
            sender_name = "alice",
            name        = "bob",
            command     = "echo",
            path        = "a/b",
            url         = "https://h/p",
        )

        assert len(item.fields) == 8
        assert all(_MASKED.fullmatch(str(value)) for value in item.fields.values())
        for raw in ("private", "alice", "echo", "https://h/p"):
            assert raw not in str(item)

    @pytest.mark.parametrize(
        "field_name",
        [
            "system_prompt",
            "user_prompt",
            "history",
            "dialogue",
            "model_response",
            "raw_output",
            "current_text",
            "goal",
            "hint",
            "description",
            "marker",
            "term",
            "emotion_tags",
            "evidence",
            "summary",
            "query",
            "host",
            "payload",
        ],
    )
    def test_free_text_keys_never_show_a_token_shaped_value(self, field_name: str) -> None:
        canary = "XIAOQING_PRIVATE_PROMPT_CANARY"

        item = LogEvent("x", **{field_name: canary})

        assert _MASKED.fullmatch(str(item.fields[field_name]))
        assert canary not in str(item)

    def test_everything_under_a_body_key_is_masked_and_siblings_stay_visible(self) -> None:
        canary = "XIAOQING_PRIVATE_PROMPT_CANARY"

        item = LogEvent(
            "x", status="ok", payload={"role": "user", "content": canary, "file_name": "a.txt"}
        )

        assert item.fields["status"] == "ok"
        assert _MASKED.fullmatch(str(item.fields["payload.role"]))
        assert _MASKED.fullmatch(str(item.fields["payload.content"]))
        assert _MASKED.fullmatch(str(item.fields["payload.file_name"]))
        assert canary not in str(item)

    def test_program_generated_codes_and_labels_stay_visible(self) -> None:
        item = LogEvent(
            "x",
            reason_code  = "private",
            status       = "ok",
            route        = "chat",
            choice       = "rest",
            plugin       = "pendo",
            command_name = "echo",
            message_type = "group",
            input_kind   = "photo",
            trace_id     = "ab-12.x:y/z@+_",
        )

        assert dict(item.fields) == {
            "reason_code" : "private",
            "status"      : "ok",
            "route"       : "chat",
            "choice"      : "rest",
            "plugin"      : "pendo",
            "command_name": "echo",
            "message_type": "group",
            "input_kind"  : "photo",
            "trace_id"    : "ab-12.x:y/z@+_",
        }
        assert str(item).startswith("x reason_code=private status=ok route=chat")

    def test_identifiers_and_numbers_are_written_as_they_are(self) -> None:
        item = LogEvent(
            "x", chat_id=123, user_id="456", message_id="m-1", elapsed_s=1.5, ok=True, bad=False
        )

        assert dict(item.fields) == {
            "chat_id"   : 123,
            "user_id"   : "456",
            "message_id": "m-1",
            "elapsed_s" : 1.5,
            "ok"        : True,
            "bad"       : False,
        }
        assert (
            str(item) == "x chat_id=123 user_id=456 message_id=m-1 elapsed_s=1.5 ok=true bad=false"
        )

    @pytest.mark.parametrize(
        ("make", "field", "rendered"),
        [
            pytest.param(lambda: Decimal("12.50"), Decimal("12.50"), "12.50", id="decimal"),
            pytest.param(lambda: Fraction(1, 4), 0.25, "0.25", id="fraction"),
            pytest.param(lambda: _Priority.HIGH, 3, "3", id="int-enum"),
            pytest.param(
                lambda: datetime.date(2026, 11, 16), "2026-11-16", "2026-11-16", id="date"
            ),
            pytest.param(
                lambda: datetime.datetime(
                    2026, 11, 16, 8, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
                ),
                "2026-11-16T08:30:00+08:00",
                "2026-11-16T08:30:00+08:00",
                id="datetime",
            ),
            pytest.param(lambda: datetime.time(8, 30, 5), "08:30:05", "08:30:05", id="time"),
            pytest.param(lambda: numpy.int64(7), 7, "7", id="numpy-int", marks=_needs_numpy),
            pytest.param(
                lambda: numpy.float32(0.5), 0.5, "0.5", id="numpy-float", marks=_needs_numpy
            ),
        ],
    )
    def test_other_numbers_dates_and_times_are_written_as_values(
        self, make: Callable[[], object], field: object, rendered: str
    ) -> None:
        item = LogEvent("x", value=make())

        assert item.fields["value"] == field
        assert str(item) == f"x value={rendered}"

    @pytest.mark.parametrize(
        "value",
        ["not ok", "聊天", "a=b", 'quote"d', "x" * 129, "line\nbreak"],
    )
    def test_plain_keys_with_text_that_is_not_a_safe_token_are_masked(self, value: str) -> None:
        item = LogEvent("x", label=value)

        assert _MASKED.fullmatch(str(item.fields["label"]))
        assert value not in str(item)

    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "access_token",
            "Authorization",
            "cookie",
            "password",
            "client_secret",
            "passwd",
        ],
    )
    def test_credential_keys_are_masked_whatever_the_value(self, key: str) -> None:
        item = LogEvent("x", **{key: "CANARY"})

        assert _MASKED.fullmatch(str(item.fields[key]))
        assert "CANARY" not in str(item)

    def test_a_credential_word_inside_a_longer_word_does_not_count(self) -> None:
        assert LogEvent("x", keyboard="abc", monkey="abc").fields == {
            "keyboard": "abc",
            "monkey"  : "abc",
        }

    def test_dicts_are_flattened_and_every_leaf_follows_the_same_rules(self) -> None:
        item = LogEvent(
            "x",
            meta={"hits": 3, "note": "free text", "nested": {"token": "t", "mode": "fast"}},
        )

        assert item.fields["meta.hits"] == 3
        assert _MASKED.fullmatch(str(item.fields["meta.note"]))
        assert _MASKED.fullmatch(str(item.fields["meta.nested.token"]))
        assert item.fields["meta.nested.mode"] == "fast"

    def test_hostile_dict_keys_cannot_inject_fields_or_lines(self) -> None:
        item = LogEvent("x", meta={"a b\nc=d": "v", "密码": "p"})

        rendered = str(item)
        assert "\n" not in rendered
        assert set(item.fields) == {"meta.a_b_c_d", "meta.__"}

    def test_sequences_and_sets_are_reduced_to_a_count(self) -> None:
        item = LogEvent(
            "x", ids=[1, 2, 3], pair=(1, 2), seen={1}, none=frozenset(), people=["alice"]
        )

        assert dict(item.fields) == {
            "ids"   : "[3 items]",
            "pair"  : "[2 items]",
            "seen"  : "[1 items]",
            "none"  : "[0 items]",
            "people": "[1 items]",
        }
        assert "alice" not in str(item)

    def test_exceptions_are_reduced_to_their_type_name(self) -> None:
        class Unprintable(RuntimeError):
            def __str__(self) -> str:
                raise ValueError("cannot print")

        item = LogEvent(
            "x", error=ValueError("secret text"), other=Unprintable(), stop=KeyboardInterrupt()
        )

        assert dict(item.fields) == {
            "error": "ValueError",
            "other": "Unprintable",
            "stop" : "KeyboardInterrupt",
        }
        assert "secret text" not in str(item)

    def test_a_raised_exception_is_located_at_the_deepest_plugin_frame(self) -> None:
        canary = "LOCATION-CANARY"
        # 插件里的一层回调、Core 里的一个工具函数：文件名落在仓库的 plugins/、core/ 下。
        plugin = _module_from_source(
            "def call(callback):\n    return callback()\n",
            str(REPOSITORY_ROOT / "plugins" / "frame_probe.py"),
        )
        core = _module_from_source(
            "def helper(callback):\n    return callback()\n",
            str(REPOSITORY_ROOT / "core" / "frame_probe.py"),
        )

        def fail() -> None:
            raise KeyError(canary)

        def recurse(depth: int) -> None:
            # 比回溯保留的帧数更深：位置照样从最里面找。
            if depth:
                recurse(depth - 1)
            # 插件调 Core 的工具函数，异常从更深处冒出来：位置是插件的那一帧。
            plugin["call"](lambda: core["helper"](fail))

        item = LogEvent("x", error=_raised(lambda: recurse(60)))
        core_only = LogEvent("x", error=_raised(lambda: core["helper"](fail)))

        assert item.fields["error.at"] == "plugins/frame_probe.py:2"
        assert core_only.fields["error.at"] == "core/frame_probe.py:2"
        assert canary not in str(item)

    def test_without_project_frames_the_location_prefers_a_real_file(self) -> None:
        generated = _module_from_source("def boom():\n    raise ValueError('CANARY')\n", "<string>")
        frame     = inspect.currentframe()
        assert frame is not None
        call_line = frame.f_lineno + 1
        error     = _raised(lambda: generated["boom"]())

        assert LogEvent("x", error=error).fields["error.at"] == (
            f"tests/core/test_logging.py:{call_line}"
        )

        only_generated = _module_from_source(
            "try:\n    raise ValueError('CANARY')\nexcept ValueError as caught:\n    error = caught\n",
            "<string>",
        )
        assert LogEvent("x", error=only_generated["error"]).fields["error.at"] == "<string>:2"

    @pytest.mark.parametrize(
        ("link", "cause"), [("from", "KeyError"), ("implicit", "KeyError"), ("from_none", None)]
    )
    def test_the_cause_is_the_innermost_chained_exception_unless_the_chain_is_suppressed(
        self, link: str, cause: str | None
    ) -> None:
        canary = "CAUSE-CANARY"

        def fail() -> None:
            try:
                try:
                    raise KeyError(canary)
                except KeyError:
                    raise OSError(canary)  # noqa: B904 - 隐式异常链
            except OSError as exc:
                if link == "from":
                    raise ValueError(canary) from exc
                if link == "from_none":
                    raise ValueError(canary) from None
                raise ValueError(canary)  # noqa: B904 - 隐式异常链

        item = LogEvent("x", error=_raised(fail))

        assert item.fields["error"] == "ValueError"
        assert item.fields.get("error.cause") == cause
        assert canary not in str(item)

    @pytest.mark.parametrize(
        ("fail", "details"),
        [
            pytest.param(
                lambda: _raise_with(RuntimeError("upstream CANARY"), status=504),
                {"status": 504},
                id="status",
            ),
            pytest.param(
                lambda: _raise_with(
                    RuntimeError("upstream CANARY"), response=SimpleNamespace(status_code=503)
                ),
                {"status": 503},
                id="response.status_code",
            ),
            pytest.param(
                lambda: _raise_with(
                    RuntimeError("upstream CANARY"), response=SimpleNamespace(status=502)
                ),
                {"status": 502},
                id="response.status",
            ),
            pytest.param(
                lambda: open("missing-CANARY.txt", encoding="utf-8"),
                {"errno": errno.ENOENT},
                id="errno",
            ),
            pytest.param(
                lambda: _raise_with(subprocess.CalledProcessError(3, ["tool", "CANARY"])),
                {"returncode": 3},
                id="returncode",
            ),
            pytest.param(
                lambda: json.loads('{"note": "CANARY'),
                {"lineno": 1, "colno": 10},
                id="lineno-colno",
            ),
            pytest.param(
                lambda: sqlite3.connect(":memory:").execute("SELECT * FROM missing_CANARY"),
                {"sqlite": "SQLITE_ERROR"},
                id="sqlite",
            ),
            pytest.param(
                lambda: importlib.import_module("xiaoqing_missing_module_probe"),
                {"module": "xiaoqing_missing_module_probe"},
                id="module",
            ),
        ],
    )
    def test_status_codes_and_similar_attributes_become_detail_fields(
        self, fail: Callable[[], object], details: dict[str, int | str]
    ) -> None:
        error = _raised(fail)

        item = LogEvent("x", error=error)

        assert {name: item.fields.get(f"error.{name}") for name in details} == details
        assert str(error) not in str(item)
        assert "CANARY" not in str(item)

    def test_attributes_that_raise_when_read_are_skipped(self) -> None:
        class Flaky(Exception):
            @property
            def status(self) -> int:
                raise RuntimeError("status unavailable")

            @property
            def response(self) -> object:
                raise RuntimeError("response unavailable")

        item = LogEvent("x", error=_raised(lambda: _raise_with(Flaky())))

        assert set(item.fields) == {"error", "error.at"}
        assert item.fields["error"] == "Flaky"

    def test_none_is_skipped_and_an_event_without_fields_is_just_its_name(self) -> None:
        assert dict(LogEvent("x", a=None, b=1).fields) == {"b": 1}
        assert str(LogEvent("x", a=None, b=1)) == "x b=1"
        assert str(LogEvent("startup.done")) == "startup.done"

    def test_fields_keep_the_order_they_were_given_in(self) -> None:
        assert str(LogEvent("x", b=1, a=2)) == "x b=1 a=2"

    def test_bytes_and_other_objects_are_masked_by_size_and_never_rendered(self) -> None:
        class Opaque:
            def __str__(self) -> str:
                raise RuntimeError("no str")

            def __repr__(self) -> str:
                raise RuntimeError("no repr")

        where = Path("C:/Users/alice/secret")
        item  = LogEvent(
            "x",
            blob   = b"0123456789",
            buffer = bytearray(b"abc"),
            where  = where,
            opaque = Opaque(),
        )

        assert str(item.fields["blob"]).startswith("<10B #")
        assert str(item.fields["buffer"]).startswith("<3B #")
        assert all(_MASKED.fullmatch(str(value)) for value in item.fields.values())
        assert "alice" not in str(item)
        # 路径和同一路径的字符串指纹相同，能和别处记的同一个路径对上。
        assert item.fields["where"] == LogEvent("x", path=os.fspath(where)).fields["path"]

    def test_a_field_may_be_called_name(self) -> None:
        item = LogEvent("x", name="Bob")

        assert _MASKED.fullmatch(str(item.fields["name"]))
        assert item.name == "x"

    def test_the_same_text_has_the_same_fingerprint_and_different_text_does_not(self) -> None:
        first = LogEvent("x", reply="same words")
        again = LogEvent("x", reply="same words")
        another = LogEvent("x", reply="other words")

        assert first.fields["reply"] == again.fields["reply"]
        assert first.fields["reply"] != another.fields["reply"]

        text = str(LogEvent("x", reply="小青").fields["reply"])
        encoded = str(LogEvent("x", reply="小青".encode()).fields["reply"])
        assert text.startswith("<2ch #") and encoded.startswith("<6B #")
        assert text.split("#")[1] != encoded.split("#")[1]

    def test_str_and_repr_are_the_rendered_text_and_hold_no_raw_value(self) -> None:
        canary = "CANARY words"
        item = LogEvent("reply.done", chat_id=5, reply=canary)

        assert repr(item) == str(item)
        assert str(item).startswith('reply.done chat_id=5 reply="<12ch #')
        assert canary not in repr(item)

    def test_values_with_spaces_quotes_or_equals_signs_are_json_quoted_in_the_text(self) -> None:
        item = LogEvent("x", reply="two words", items=[1], status="ok")

        assert re.fullmatch(
            r'x reply="<9ch #[0-9a-f]{8}>" items="\[1 items\]" status=ok', str(item)
        )
        assert _MASKED.fullmatch(str(item.fields["reply"]))  # 字段里是不带引号的脱敏值

    def test_the_fields_of_an_event_cannot_be_changed(self) -> None:
        item = LogEvent("x", a=1)

        with pytest.raises(TypeError):
            item.fields["a"] = 2  # type: ignore[index]

    @pytest.mark.usefixtures("restore_logging")
    def test_an_event_in_a_log_call_is_written_as_its_rendered_text(self, tmp_path: Path) -> None:
        logged = _setup(tmp_path)

        log.info(LogEvent("reply.done", chat_id=5, reply="secret words"))

        message = logged.line_with("reply.done").group("message")
        assert re.fullmatch(r'reply\.done chat_id=5 reply="<12ch #[0-9a-f]{8}>"', message)

    def test_events_are_what_caplog_hands_back_for_assertions(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            log.info(LogEvent("source.failed", source="baike", error=OSError("down")))

        (record,) = caplog.records
        assert isinstance(record.msg, LogEvent)
        assert record.msg.name == "source.failed"
        assert dict(record.msg.fields) == {"source": "baike", "error": "OSError"}


# ============================================================
# 配置校验
# ============================================================


class TestLoggingSettings:
    def test_defaults(self) -> None:
        assert LoggingSettings().model_dump() == {
            "level"    : "INFO",
            "levels"   : {},
            "console"  : True,
            "file"     : True,
            "keep_days": 5,
        }

    @pytest.mark.parametrize(
        "raw",
        [
            {"level": "TRACE"},
            {"level": "info"},
            {"colour": True},
            {"console": "yes"},
            {"file": 1},
            {"keep_days": 0},
            {"keep_days": 366},
            {"keep_days": "many"},
            {"levels": {"plugins.demo": "LOUD"}},
            {"levels": {"plugins.demo": 10}},
            {"levels": ["plugins.demo"]},
        ],
    )
    def test_invalid_blocks_are_rejected(self, raw: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            LoggingSettings.model_validate(raw)

    def test_settings_cannot_be_changed_after_parsing(self) -> None:
        settings = LoggingSettings()

        with pytest.raises(ValidationError):
            settings.level = "DEBUG"  # type: ignore[misc]

    def test_the_application_parses_the_config_block_once_and_hands_it_to_setup(
        self, temp_app_root: Path
    ) -> None:
        config_path = temp_app_root / "config" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["logging"] = {
            "level"    : "DEBUG",
            "levels"   : {"plugins.demo": "INFO"},
            "console"  : False,
            "keep_days": 2,
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")

        with patch("core.app.setup_logging") as setup:
            XiaoQingApp(temp_app_root)

        setup.assert_called_once_with(
            LoggingSettings(
                level="DEBUG", levels={"plugins.demo": "INFO"}, console=False, keep_days=2
            ),
            temp_app_root / "logs",
        )


# ============================================================
# 入站消息、定时任务、生命周期钩子里的 request id
# ============================================================

_PROBE_COMMAND_SOURCE = """
import logging

from core.log import LogEvent
from tests.helpers.plugin_probe import PROBE

log = logging.getLogger(__name__)


async def handle(command, args, message_event, context):
    PROBE.log("request_ids").append(context.request_id)
    log.info(LogEvent("probe.ran"))
    return [{"type": "text", "data": {"text": "ok"}}]
"""

_SCHEDULED_SOURCE = """
import logging

from core.log import LogEvent
from tests.helpers.plugin_probe import PROBE

log = logging.getLogger(__name__)


async def scheduled(context):
    PROBE.log("request_ids").append(context.request_id)
    log.info(LogEvent("scheduled.ran"))
    return None
"""

_HOOKS_SOURCE = """
import asyncio
import logging

from core.log import LogEvent
from tests.helpers.plugin_probe import PROBE

log = logging.getLogger(__name__)


async def _background(label):
    await asyncio.sleep(0)
    log.info(LogEvent("background.tick", label=label))


async def init(context):
    PROBE.log("tasks").append(asyncio.create_task(_background("started_by_init")))


async def shutdown(context):
    PROBE.log("tasks").append(asyncio.create_task(_background("started_by_shutdown")))
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "concurrency"),
    [
        pytest.param(_PROBE_COMMAND_SOURCE, "parallel", id="async-parallel"),
        pytest.param(_PROBE_COMMAND_SOURCE, "sequential", id="async-sequential"),
        pytest.param(
            _PROBE_COMMAND_SOURCE.replace("async def handle", "def handle"),
            "sequential",
            id="sync-sequential",
        ),
    ],
)
async def test_an_inbound_message_logs_with_one_id_in_the_dispatcher_and_the_plugin(
    temp_app_root: Path, logged_lines: LoggedLines, source: str, concurrency: str
) -> None:
    app                               = XiaoQingApp(temp_app_root)
    app.ws_client                     = MagicMock()
    app.ws_client.credentials_trusted = True
    app.ws_client.connected = Mock(return_value=True)
    app.ws_client.send_action = AsyncMock(return_value=True)
    await load_test_plugin(
        app,
        "probe",
        source,
        commands    = [command_declaration("probe")],
        concurrency = concurrency,
    )

    # 上游 WebSocket 事件的 request id 由客户端收到事件时分配，所以从客户端进入。
    client = OneBotWsClient("ws://example.test/socket", "")
    await client._dispatch_event(
        app._handle_upstream_event,
        {
            "post_type"   : "message",
            "message_type": "group",
            "user_id"     : 12345,
            "group_id"    : 67890,
            "message"     : "/probe",
        },
    )
    await wait_until(lambda: app.ws_client.send_action.await_count == 1)
    await client.stop()

    (context_request_id,) = PROBE.log("request_ids")
    assert logged_lines.request_id_of("message.received") == context_request_id
    assert logged_lines.request_id_of("command.matched") == context_request_id
    assert logged_lines.request_id_of("probe.ran") == context_request_id
    assert logged_lines.request_id_of("send.start") == context_request_id
    app.ws_client.send_action.assert_awaited_once()
    assert context_request_id != "-"
    assert logged_lines.line_with("probe.ran").group("logger").startswith("plugins.probe")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["websocket", "http-actions"])
async def test_an_inbound_event_and_its_reply_receipt_log_with_one_id(
    unused_tcp_port: int, logged_lines: LoggedLines, transport: str
) -> None:
    log       = logging.getLogger("plugins.probe")
    committed = asyncio.Event()

    async def commit() -> None:
        log.info(LogEvent("probe.committed"))
        committed.set()

    async def handler(event: dict[str, Any]) -> list[dict[str, Any]]:
        log.info(LogEvent("probe.handled"))
        receipt = DeliveryReceipt(
            expected_actions=1, commit=commit, rollback=commit, unknown=commit
        )
        return [attach_receipt({"action": "send_private_msg", "params": {}}, receipt)]

    async with serving(make_server(handler, unused_tcp_port)) as session:
        if transport == "websocket":
            ws = await session.ws_connect("/ws", headers=auth_headers())
            await ws.send_json(onebot_message_payload())
            await ws.receive_json(timeout=2)
        else:
            # actions 响应模式：动作列表写进响应正文，响应写完后才结算回执。
            assert (await post_event(session, onebot_message_payload())).status == 200
        await asyncio.wait_for(committed.wait(), timeout=2)

    handled_id = logged_lines.request_id_of("probe.handled")
    assert handled_id != "-"
    assert logged_lines.request_id_of("probe.committed") == handled_id


@pytest.mark.asyncio
async def test_a_scheduled_run_gets_its_own_id_shared_by_the_context_and_the_log(
    temp_app_root: Path, logged_lines: LoggedLines
) -> None:
    app = XiaoQingApp(temp_app_root)
    try:
        await load_test_plugin(
            app,
            "probe",
            _SCHEDULED_SOURCE,
            schedule=[{"id": "poll", "handler": "scheduled", "cron": {"minute": "*"}}],
        )
        app._reschedule("startup")
        assert app.scheduler.scheduler is not None
        (job,) = app.scheduler.scheduler.get_jobs()

        await job.func()
        await job.func()
    finally:
        app.scheduler.shutdown()

    first, second = PROBE.log("request_ids")
    assert first != second
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert logged_lines.request_ids_of("scheduled.ran") == [first, second]
    assert current_request_id() is None


@pytest.mark.asyncio
async def test_background_work_started_from_a_command_does_not_inherit_its_id(
    temp_app_root: Path, logged_lines: LoggedLines
) -> None:
    app                                   = XiaoQingApp(temp_app_root)
    config_callback_ids: list[str | None] = []
    app.config_manager.on_reload(lambda _snapshot: config_callback_ids.append(current_request_id()))

    with log_scope("command-17"):
        await load_test_plugin(app, "hooks", _HOOKS_SOURCE)
        await app.plugin_manager.unload_plugin("hooks")
        # /reload 在线程里重载配置；配置回调据此重启的常驻任务同样不能带这条命令的 id。
        await asyncio.to_thread(app.config_manager.update_secret, "admin_user_ids", [2033])
    await asyncio.gather(*PROBE.log("tasks"))
    await wait_until(lambda: bool(config_callback_ids))

    assert logged_lines.request_id_of("started_by_init") == "-"
    assert logged_lines.request_id_of("started_by_shutdown") == "-"
    assert config_callback_ids == [None]


# ============================================================
# 事件循环上没人处理的异常
# ============================================================

_ASYNCIO_CANARY = "Authorization: Bearer ASYNCIO-CANARY"


def _raise_asyncio_canary() -> None:
    raise ValueError(_ASYNCIO_CANARY)


async def _unretrieved_task_exception() -> None:
    async def fail() -> None:
        _raise_asyncio_canary()

    task = asyncio.create_task(fail(), name="probe-task")
    while not task.done():
        await asyncio.sleep(0)
    del task
    gc.collect()


async def _unretrieved_future_exception() -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_exception(ValueError(_ASYNCIO_CANARY))
    del future
    gc.collect()


async def _failing_callback() -> None:
    asyncio.get_running_loop().call_soon(_raise_asyncio_canary)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _pending_task_destroyed() -> None:
    async def wait_forever() -> None:
        await asyncio.get_running_loop().create_future()

    task = asyncio.create_task(wait_forever(), name="stuck-task")
    await asyncio.sleep(0)
    del task
    gc.collect()


async def _unclosed_client_session() -> None:
    session = aiohttp.ClientSession(headers={"Authorization": _ASYNCIO_CANARY})
    del session
    gc.collect()


async def _other_loop_error() -> None:
    asyncio.get_running_loop().call_exception_handler(
        {"message": _ASYNCIO_CANARY, "exception": ValueError(_ASYNCIO_CANARY)}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "kind", "task", "error"),
    [
        pytest.param(_unretrieved_task_exception, "task_exception", "probe-task", "ValueError"),
        pytest.param(_unretrieved_future_exception, "future_exception", None, "ValueError"),
        pytest.param(_failing_callback, "callback_exception", None, "ValueError"),
        pytest.param(_pending_task_destroyed, "pending_task_destroyed", "stuck-task", None),
        pytest.param(
            _unclosed_client_session,
            "unclosed_resource",
            None,
            None,
            marks=pytest.mark.filterwarnings("ignore::ResourceWarning"),
        ),
        pytest.param(_other_loop_error, "other", None, "ValueError"),
    ],
)
async def test_loop_errors_nobody_handled_are_logged_as_events_once_the_app_runs(
    temp_app_root: Path,
    logged_lines: LoggedLines,
    trigger: Callable[[], Any],
    kind: str,
    task: str | None,
    error: str | None,
) -> None:
    app = XiaoQingApp(temp_app_root)
    await app.start()
    try:
        await trigger()
    finally:
        await app.stop()

    line = logged_lines.line_with("asyncio.unhandled")
    name, fields = event_fields(line["message"])
    assert (line["level"], line["logger"], name) == ("ERROR", "asyncio", "asyncio.unhandled")
    assert (fields["kind"], fields.get("task"), fields.get("error")) == (kind, task, error)
    assert "ASYNCIO-CANARY" not in "\n".join(logged_lines.lines())
