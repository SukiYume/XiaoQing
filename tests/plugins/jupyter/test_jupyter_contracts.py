"""Jupyter 命令、REPL 状态与公开响应的边界契约。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.session import Session
from plugins.jupyter import main as jupyter
from plugins.jupyter.jupyter_config import (
    MAX_CODE_BYTES,
    MAX_CODE_CHARS,
    MAX_REPL_EXECUTIONS,
    MAX_REPL_LINE_CHARS,
    MAX_REPL_LINES,
    MAX_REPL_PREVIEW_CHARS,
    REPL_SESSION_TIMEOUT,
)
from plugins.jupyter.jupyter_models import ExecutionResult


def _text(response: list[dict[str, Any]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in response
        if segment.get("type") == "text"
    )


def _session(
    lines: list[str] | object | None = None,
    execution_count: object = 0,
    *,
    plugin_name: str = "jupyter",
) -> Session:
    return Session(
        user_id=1,
        group_id=2,
        plugin_name=plugin_name,
        data={
            "code_buffer": [] if lines is None else lines,
            "execution_count": execution_count,
        },
    )


class ReplContext:
    def __init__(self, tmp_path: Path, existing: object | None = None) -> None:
        self.data_dir = tmp_path
        self.current_user_id = 1
        self.current_group_id = 2
        self.request_id = "req-jupyter-contract"
        self.existing = existing
        self.created: tuple[dict[str, object], float] | None = None
        self.end_calls = 0

    async def get_session(self) -> object | None:
        return self.existing

    async def create_session(self, *, initial_data: dict[str, object], timeout: float) -> None:
        self.created = (initial_data, timeout)

    async def end_session(self) -> bool:
        self.end_calls += 1
        return True


class KernelManagerStub:
    def __init__(self, *, running: bool = False, fail: str | None = None) -> None:
        self.running = running
        self.fail = fail
        self.calls: list[str] = []
        self.monitor_calls = 0

    def get_status(self) -> dict[str, object]:
        return {"running": self.running, "message": "测试状态"}

    def _record(self, action: str) -> None:
        self.calls.append(action)
        if self.fail == action:
            raise RuntimeError("private lifecycle detail")

    def start_kernel(self) -> None:
        self._record("start")

    def restart_kernel(self) -> None:
        self._record("restart")

    def shutdown_kernel(self) -> None:
        self._record("shutdown")

    def ensure_idle_monitor(self) -> None:
        self.monitor_calls += 1


@pytest.mark.parametrize(
    ("value", "allow_empty", "error_type"),
    (
        (None, True, TypeError),
        ("", False, jupyter.JupyterCommandError),
        ("print(1)\x00", True, jupyter.JupyterCommandError),
        ("x" * (MAX_CODE_CHARS + 1), True, jupyter.JupyterCommandError),
        ("界" * ((MAX_CODE_BYTES // 3) + 1), True, jupyter.JupyterCommandError),
    ),
    ids=("type", "empty", "control", "char-limit", "byte-limit"),
)
def test_code_validation_rejects_invalid_shape_and_budgets(value, allow_empty, error_type):
    with pytest.raises(error_type):
        jupyter._validate_code_text(value, allow_empty=allow_empty)


@pytest.mark.parametrize(
    ("args", "expected"),
    (("help", "help"), (" 帮助 ", "help"), ("repl", "repl"), ("print(1)", "execute")),
)
def test_main_action_parser(args: str, expected: str) -> None:
    assert jupyter._parse_main_action(args) == expected


def test_main_action_parser_requires_string() -> None:
    with pytest.raises(TypeError):
        jupyter._parse_main_action(None)


@pytest.mark.parametrize(
    ("args", "expected"),
    (
        ("", "status"),
        ("状态", "status"),
        ("启动", "start"),
        ("重启", "restart"),
        ("stop", "shutdown"),
        ("-h", "help"),
    ),
)
def test_kernel_action_parser_accepts_only_known_single_action(args: str, expected: str) -> None:
    assert jupyter._parse_kernel_action(args) == expected


@pytest.mark.parametrize(
    "args",
    (None, "x" * 65, "status\x00", '"unterminated', "status extra", "destroy"),
)
def test_kernel_action_parser_rejects_ambiguous_input(args) -> None:
    with pytest.raises((TypeError, jupyter.JupyterCommandError)):
        jupyter._parse_kernel_action(args)


def test_owner_key_rejects_invalid_group_identity() -> None:
    with pytest.raises(RuntimeError):
        jupyter._owner_key(SimpleNamespace(current_user_id=1, current_group_id="2"))


def test_execution_result_formats_all_fields_and_exactly_truncates() -> None:
    result = ExecutionResult(
        stdout=" out ",
        stderr=" warn ",
        result="42",
        error="failed",
        execution_time=1.25,
    )
    rendered = result.format_output()
    assert "out" in rendered
    assert ">>> 42" in rendered
    assert "warn" in rendered
    assert "failed" in rendered
    assert "1.25s" in rendered

    oversized = ExecutionResult(stdout="x" * 10_000).format_output()
    assert len(oversized) == 2_000
    assert oversized.endswith("（输出已截断）")
    assert "⏱️" not in ExecutionResult(execution_time=float("nan")).format_output()


def test_result_segments_keep_headers_footers_and_skip_invalid_images() -> None:
    response = jupyter._build_result_segments(
        ExecutionResult(images=[b"not-a-png"]),
        header="header",
        footer="footer",
    )
    assert _text(response).startswith("header")
    assert _text(response).endswith("footer")
    assert [segment for segment in response if segment["type"] == "image"] == []


@pytest.mark.asyncio
async def test_empty_execute_request_returns_usage_without_manager(tmp_path: Path) -> None:
    response = await jupyter._handle_execute(
        "  ",
        SimpleNamespace(data_dir=tmp_path, current_user_id=1, current_group_id=2),
    )
    assert "请输入" in _text(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected", "monitor_calls"),
    (("start", "已启动", 1), ("restart", "已重启", 1), ("shutdown", "已关闭", 0)),
)
async def test_kernel_lifecycle_actions_have_precise_success_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected: str,
    monitor_calls: int,
) -> None:
    manager = KernelManagerStub()
    monkeypatch.setattr(jupyter.JupyterKernelManager, "get_instance", lambda *_args: manager)
    context = ReplContext(tmp_path)

    response = await jupyter._handle_kernel(action, context)

    assert manager.calls == [action]
    assert manager.monitor_calls == monitor_calls
    assert expected in _text(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(("running", "marker"), ((True, "🟢"), (False, "⚫")))
async def test_kernel_status_marker_matches_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
    marker: str,
) -> None:
    manager = KernelManagerStub(running=running)
    monkeypatch.setattr(jupyter.JupyterKernelManager, "get_instance", lambda *_args: manager)
    response = await jupyter._handle_kernel("status", ReplContext(tmp_path))
    assert _text(response).startswith(marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    (("start", "操作失败"), ("shutdown", "无法确认")),
)
async def test_kernel_lifecycle_errors_do_not_expose_exception_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected: str,
) -> None:
    manager = KernelManagerStub(fail=action)
    monkeypatch.setattr(jupyter.JupyterKernelManager, "get_instance", lambda *_args: manager)
    response = await jupyter._handle_kernel(action, ReplContext(tmp_path))
    assert expected in _text(response)
    assert "private lifecycle detail" not in _text(response)


@pytest.mark.parametrize(
    ("lines", "count"),
    (
        ("not-a-list", 0),
        (["ok"], True),
        ([1], 0),
        (["bad\nline"], 0),
        (["x" * (MAX_REPL_LINE_CHARS + 1)], 0),
        (["x"] * (MAX_REPL_LINES + 1), 0),
        (["界" * ((MAX_CODE_BYTES // 3) + 1)], 0),
    ),
)
def test_repl_state_loader_rejects_corrupt_or_oversized_state(lines, count) -> None:
    with pytest.raises(jupyter.InvalidReplState):
        jupyter._load_repl_state(_session(lines, count))


def test_repl_preview_is_independently_bounded() -> None:
    short = "print(1)"
    assert jupyter._code_preview(short) == short
    preview = jupyter._code_preview("x" * (MAX_REPL_PREVIEW_CHARS + 100))
    assert len(preview) == MAX_REPL_PREVIEW_CHARS
    assert "预览已截断" in preview


@pytest.mark.parametrize(
    "value",
    (
        None,
        "x" * (MAX_REPL_LINE_CHARS + 1),
        "bad\x00line",
    ),
)
def test_repl_append_rejects_invalid_line(value) -> None:
    error = TypeError if value is None else jupyter.JupyterCommandError
    with pytest.raises(error):
        jupyter._append_repl_text(jupyter.ReplState((), 0), value)


def test_repl_append_rejects_line_and_total_code_limits() -> None:
    full = jupyter.ReplState(tuple("x" for _ in range(MAX_REPL_LINES)), 0)
    with pytest.raises(jupyter.JupyterCommandError):
        jupyter._append_repl_text(full, "extra")
    near_byte_limit = jupyter.ReplState(("界" * (MAX_CODE_BYTES // 3),), 0)
    with pytest.raises(jupyter.JupyterCommandError):
        jupyter._append_repl_text(near_byte_limit, "界")


@pytest.mark.asyncio
async def test_start_repl_creates_minimal_bounded_session(tmp_path: Path) -> None:
    context = ReplContext(tmp_path)
    response = await jupyter._start_repl_session(context)
    assert context.created == (
        {"code_buffer": [], "execution_count": 0},
        REPL_SESSION_TIMEOUT,
    )
    assert "已启动" in _text(response)


@pytest.mark.asyncio
async def test_start_repl_reuses_valid_session_without_replacing_it(tmp_path: Path) -> None:
    existing = _session([f"line-{index}" for index in range(7)])
    context = ReplContext(tmp_path, existing)
    response = await jupyter._start_repl_session(context)
    assert context.created is None
    assert "line-2" in _text(response)
    assert "line-0" not in _text(response)


@pytest.mark.asyncio
async def test_start_repl_discards_only_invalid_jupyter_session(tmp_path: Path) -> None:
    context = ReplContext(tmp_path, _session("corrupt"))
    response = await jupyter._start_repl_session(context)
    assert context.end_calls == 1
    assert context.created is not None
    assert "已启动" in _text(response)


@pytest.mark.asyncio
async def test_execute_repl_handles_empty_limit_and_internal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ReplContext(tmp_path)
    session = _session([])
    assert "缓冲区为空" in _text(
        await jupyter._execute_repl_buffer(jupyter.ReplState((), 0), session, context)
    )
    assert "计数已达上限" in _text(
        await jupyter._execute_repl_buffer(
            jupyter.ReplState(("print(1)",), MAX_REPL_EXECUTIONS),
            session,
            context,
        )
    )

    class BrokenManager:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("private repl detail")

    monkeypatch.setattr(
        jupyter.JupyterKernelManager, "get_instance", lambda *_args: BrokenManager()
    )
    response = await jupyter._execute_repl_buffer(
        jupyter.ReplState(("print(1)",), 0),
        _session(["print(1)"]),
        context,
    )
    assert "缓冲区已保留" in _text(response)
    assert "private repl detail" not in _text(response)


@pytest.mark.asyncio
async def test_handle_session_management_actions_and_invalid_state(tmp_path: Path) -> None:
    context = ReplContext(tmp_path)
    session = _session(["print(1)"])

    assert "当前缓冲区" in _text(await jupyter.handle_session("show", {}, context, session))
    assert "REPL 帮助" in _text(await jupyter.handle_session("help", {}, context, session))
    assert "已清空" in _text(await jupyter.handle_session("clear", {}, context, session))
    assert "缓冲区为空" in _text(await jupyter.handle_session("show", {}, context, session))

    corrupt = _session("corrupt")
    response = await jupyter.handle_session("show", {}, context, corrupt)
    assert context.end_calls == 1
    assert "状态无效" in _text(response)


@pytest.mark.asyncio
async def test_repl_reserved_word_with_leading_space_is_appended_as_code(tmp_path: Path) -> None:
    context = ReplContext(tmp_path)
    session = _session([])

    response = await jupyter.handle_session(" run", {}, context, session)

    assert "已添加" in _text(response)
    assert session.get("code_buffer") == [" run"]


@pytest.mark.asyncio
async def test_handle_session_rejects_foreign_or_non_string_input(tmp_path: Path) -> None:
    context = ReplContext(tmp_path)
    with pytest.raises(ValueError):
        await jupyter.handle_session("show", {}, context, _session(plugin_name="echo"))
    with pytest.raises(TypeError):
        await jupyter.handle_session(None, {}, context, _session())


@pytest.mark.asyncio
async def test_handle_session_returns_boundary_error_without_mutating_buffer(
    tmp_path: Path,
) -> None:
    context = ReplContext(tmp_path)
    session = _session([])
    response = await jupyter.handle_session(
        "x" * (MAX_REPL_LINE_CHARS + 1),
        {},
        context,
        session,
    )
    assert "单行不能超过" in _text(response)
    assert session.get("code_buffer") == []


@pytest.mark.asyncio
async def test_top_level_handle_dependency_and_input_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ReplContext(tmp_path)
    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: False)
    assert "依赖不可用" in _text(await jupyter.handle("jupyter", "print(1)", {}, context))
    assert "依赖不可用" in _text(await jupyter.handle("jupyter_kernel", "status", {}, context))

    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: True)
    assert "timeout" in _text(await jupyter.handle("jupyter", "-t nope", {}, context))
    assert "未知内核操作" in _text(await jupyter.handle("jupyter_kernel", "destroy", {}, context))
    assert "处理请求失败" in _text(await jupyter.handle("jupyter", None, {}, context))


@pytest.mark.asyncio
async def test_top_level_handle_routes_repl_and_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ReplContext(tmp_path)
    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: True)

    class Manager:
        async def execute(self, code: str, **_kwargs) -> ExecutionResult:
            return ExecutionResult(stdout=code)

    monkeypatch.setattr(jupyter.JupyterKernelManager, "get_instance", lambda *_args: Manager())
    assert "REPL 已启动" in _text(await jupyter.handle("jupyter", "repl", {}, context))
    assert "print(1)" in _text(await jupyter.handle("jupyter", "print(1)", {}, context))


@pytest.mark.asyncio
async def test_shutdown_skips_missing_dependency_and_contains_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ReplContext(tmp_path)
    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: False)
    assert await jupyter.shutdown(context) is None

    async def fail_shutdown() -> None:
        raise RuntimeError("private shutdown detail")

    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: True)
    monkeypatch.setattr(jupyter.JupyterKernelManager, "shutdown_all_async", fail_shutdown)
    assert await jupyter.shutdown(context) is None


@pytest.mark.asyncio
async def test_shutdown_success_calls_manager_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def shutdown_all() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(jupyter, "_dependencies_available", lambda: True)
    monkeypatch.setattr(jupyter.JupyterKernelManager, "shutdown_all_async", shutdown_all)
    await jupyter.shutdown(ReplContext(tmp_path))
    assert calls == 1


def test_missing_dependency_probe_is_cached_until_plugin_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def probe() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(jupyter, "_DEPENDENCY_PROBED", False)
    monkeypatch.setattr(jupyter.jupyter_manager, "lazy_import_jupyter", probe)
    monkeypatch.setattr(jupyter.jupyter_manager, "JUPYTER_AVAILABLE", False)
    monkeypatch.setattr(jupyter.jupyter_manager, "KernelManager", None)

    assert jupyter._dependencies_available() is False
    assert jupyter._dependencies_available() is False
    assert calls == 1
