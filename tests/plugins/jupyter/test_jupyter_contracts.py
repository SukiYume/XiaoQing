"""Jupyter 命令、REPL 状态与公开响应的边界契约。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.delivery import CommandOutcome
from core.session import Session
from plugins.jupyter import jupyter_manager as manager_module
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
from plugins.jupyter.jupyter_manager import JupyterKernelManager
from plugins.jupyter.jupyter_models import ExecutionResult
from tests.helpers.jupyter_test_support import KernelBackend, install_kernel_backend


def _text(response: list[dict[str, Any]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in response
        if segment.get("type") == "text"
    )


def _session(
    lines: list[str] | object | None = None,
    execution_count: object          = 0,
    *,
    plugin_name: str = "jupyter",
) -> Session:
    return Session(
        user_id     = 1,
        group_id    = 2,
        plugin_name = plugin_name,
        data        = {
            "code_buffer"    : [] if lines is None else lines,
            "execution_count": execution_count,
        },
    )


class ReplContext:
    def __init__(self, tmp_path: Path, existing: object | None = None) -> None:
        self.data_dir                                        = tmp_path
        self.current_user_id                                 = 1
        self.current_group_id                                = 2
        self.request_id                                      = "req-jupyter-contract"
        self.existing                                        = existing
        self.created: tuple[dict[str, object], float] | None = None
        self.end_calls                                       = 0

    async def get_session(self) -> object | None:
        return self.existing

    async def create_session(self, *, initial_data: dict[str, object], timeout: float) -> None:
        self.created = (initial_data, timeout)

    async def end_session(self) -> bool:
        self.end_calls += 1
        return True


@pytest_asyncio.fixture
async def kernel_backend(monkeypatch: pytest.MonkeyPatch):
    """依赖可用，内核由替身承担；用例结束时关掉全部内核并清空实例注册表。"""

    backend = KernelBackend()
    install_kernel_backend(monkeypatch, backend)
    await JupyterKernelManager.shutdown_all_async()
    yield backend
    for kernel in backend.kernels:
        kernel.shutdown_fails = None
    await JupyterKernelManager.shutdown_all_async()


def _remove_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 jupyter_client 的导入真实失败，并清掉插件已缓存的探测结果。"""

    monkeypatch.setitem(sys.modules, "jupyter_client", None)
    monkeypatch.setattr(manager_module, "JUPYTER_AVAILABLE", False)
    monkeypatch.setattr(manager_module, "IMPORT_ERROR", None)
    monkeypatch.setattr(manager_module, "KernelManager", None)
    monkeypatch.setattr(jupyter, "_DEPENDENCY_PROBED", False)


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
        stdout         = " out ",
        stderr         = " warn ",
        result         = "42",
        error          = "failed",
        execution_time = 1.25,
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
        header = "header",
        footer = "footer",
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
    ("action", "expected"),
    (("start", "已启动"), ("restart", "已重启"), ("shutdown", "已关闭")),
)
async def test_kernel_lifecycle_actions_have_precise_success_messages(
    tmp_path: Path,
    kernel_backend: KernelBackend,
    action: str,
    expected: str,
) -> None:
    manager = JupyterKernelManager.get_instance(tmp_path, "user:1:group:2")
    if action != "start":
        manager.start_kernel()

    response = await jupyter.handle("jupyter_kernel", action, {}, ReplContext(tmp_path))

    assert expected in _text(response)
    kernels = kernel_backend.kernels
    if action == "start":
        assert [kernel.alive for kernel in kernels] == [True]
        assert manager._shutdown_task is not None  # noqa: SLF001 - 启动后登记了空闲监视器。
    elif action == "restart":
        assert [kernel.alive for kernel in kernels] == [False, True]
        assert manager._shutdown_task is not None  # noqa: SLF001
    else:
        assert [kernel.alive for kernel in kernels] == [False]
        assert manager._shutdown_task is None  # noqa: SLF001 - 关闭后不再登记监视器。


@pytest.mark.asyncio
@pytest.mark.parametrize(("running", "marker"), ((True, "🟢"), (False, "⚫")))
async def test_kernel_status_marker_matches_state(
    tmp_path: Path,
    kernel_backend: KernelBackend,
    running: bool,
    marker: str,
) -> None:
    if running:
        JupyterKernelManager.get_instance(tmp_path, "user:1:group:2").start_kernel()
    response = await jupyter.handle("jupyter_kernel", "status", {}, ReplContext(tmp_path))
    assert _text(response).startswith(marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    (("start", "操作失败"), ("shutdown", "无法确认")),
)
async def test_kernel_lifecycle_errors_do_not_expose_exception_body(
    tmp_path: Path,
    kernel_backend: KernelBackend,
    action: str,
    expected: str,
) -> None:
    if action == "start":
        kernel_backend.fail_start = "private lifecycle detail"
    else:
        JupyterKernelManager.get_instance(tmp_path, "user:1:group:2").start_kernel()
        kernel_backend.kernels[0].shutdown_fails = "private lifecycle detail"
    response = await jupyter.handle("jupyter_kernel", action, {}, ReplContext(tmp_path))
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
    context  = ReplContext(tmp_path)
    response = await jupyter._start_repl_session(context)
    assert context.created == (
        {"code_buffer": [], "execution_count": 0},
        REPL_SESSION_TIMEOUT,
    )
    assert "已启动" in _text(response)


@pytest.mark.asyncio
async def test_start_repl_reuses_valid_session_without_replacing_it(tmp_path: Path) -> None:
    existing = _session([f"line-{index}" for index in range(7)])
    context  = ReplContext(tmp_path, existing)
    response = await jupyter._start_repl_session(context)
    assert context.created is None
    assert "line-2" in _text(response)
    assert "line-0" not in _text(response)


@pytest.mark.asyncio
async def test_start_repl_discards_only_invalid_jupyter_session(tmp_path: Path) -> None:
    context  = ReplContext(tmp_path, _session("corrupt"))
    response = await jupyter._start_repl_session(context)
    assert context.end_calls == 1
    assert context.created is not None
    assert "已启动" in _text(response)


@pytest.mark.asyncio
async def test_execute_repl_handles_empty_limit_and_internal_failure(tmp_path: Path) -> None:
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

    # 身份无效时拿不到内核实例：执行失败，缓冲区保留，异常正文不外露。
    context.current_user_id = None
    response                = await jupyter._execute_repl_buffer(
        jupyter.ReplState(("print(1)",), 0),
        _session(["print(1)"]),
        context,
    )
    assert "缓冲区已保留" in _text(response)
    assert "exact positive user id" not in _text(response)


@pytest.mark.asyncio
async def test_handle_session_management_actions_and_invalid_state(tmp_path: Path) -> None:
    context = ReplContext(tmp_path)
    session = _session(["print(1)"])

    assert "当前缓冲区" in _text(await jupyter.handle_session("show", {}, context, session))
    assert "REPL 帮助" in _text(await jupyter.handle_session("help", {}, context, session))
    assert "已清空" in _text(await jupyter.handle_session("clear", {}, context, session))
    assert "缓冲区为空" in _text(await jupyter.handle_session("show", {}, context, session))

    corrupt  = _session("corrupt")
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
    context  = ReplContext(tmp_path)
    session  = _session([])
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
    kernel_backend: KernelBackend,
) -> None:
    context = ReplContext(tmp_path)
    _remove_optional_dependency(monkeypatch)
    assert "依赖不可用" in _text(await jupyter.handle("jupyter", "print(1)", {}, context))
    assert "依赖不可用" in _text(await jupyter.handle("jupyter_kernel", "status", {}, context))

    monkeypatch.setattr(manager_module, "JUPYTER_AVAILABLE", True)
    monkeypatch.setattr(manager_module, "KernelManager", kernel_backend)
    assert "timeout" in _text(await jupyter.handle("jupyter", "-t nope", {}, context))
    assert "未知内核操作" in _text(await jupyter.handle("jupyter_kernel", "destroy", {}, context))
    unexpected = await jupyter.handle("jupyter", None, {}, context)
    assert getattr(unexpected, "command_outcome", None) == CommandOutcome("error")


@pytest.mark.asyncio
async def test_top_level_handle_routes_repl_and_execute(
    tmp_path: Path,
    kernel_backend: KernelBackend,
) -> None:
    context                  = ReplContext(tmp_path)
    kernel_backend.responder = lambda code: [
        {"msg_type": "stream", "content": {"name": "stdout", "text": code}}
    ]
    assert "REPL 已启动" in _text(await jupyter.handle("jupyter", "repl", {}, context))
    assert "print(1)" in _text(await jupyter.handle("jupyter", "print(1)", {}, context))


@pytest.mark.asyncio
async def test_shutdown_skips_missing_dependency_and_contains_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kernel_backend: KernelBackend,
) -> None:
    _remove_optional_dependency(monkeypatch)
    assert await jupyter.shutdown() is None
    assert kernel_backend.kernels == []

    monkeypatch.setattr(manager_module, "JUPYTER_AVAILABLE", True)
    monkeypatch.setattr(manager_module, "KernelManager", kernel_backend)
    JupyterKernelManager.get_instance(tmp_path, "user:1:group:2").start_kernel()
    kernel_backend.kernels[0].shutdown_fails = "private shutdown detail"
    assert await jupyter.shutdown() is None
    assert kernel_backend.kernels[0].shutdown_calls == 1


@pytest.mark.asyncio
async def test_shutdown_success_cleans_up_registered_kernels_once(
    tmp_path: Path,
    kernel_backend: KernelBackend,
) -> None:
    JupyterKernelManager.get_instance(tmp_path, "user:1:group:2").start_kernel()

    await jupyter.shutdown()

    kernel = kernel_backend.kernels[0]
    assert kernel.shutdown_calls == 1
    assert kernel.alive is False
    assert JupyterKernelManager._instances == {}  # noqa: SLF001


def test_missing_dependency_probe_is_cached_until_plugin_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_optional_dependency(monkeypatch)
    assert jupyter._dependencies_available() is False

    # 依赖随后装好，探测结果在插件重新 init 之前保持不变。
    installed               = ModuleType("jupyter_client")
    installed.KernelManager = KernelBackend()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jupyter_client", installed)
    assert jupyter._dependencies_available() is False

    jupyter.init()
    assert jupyter._dependencies_available() is True
