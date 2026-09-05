from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.sensitive_audit import summarize_sensitive
from plugins.jupyter import main as jupyter_main
from plugins.jupyter.jupyter_models import ExecutionResult
from plugins.shell import main as shell_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT           = REPOSITORY_ROOT
COMMAND_CANARY = "CR220_SHELL_TOKEN_CANARY"
CODE_CANARY    = "CR220_JUPYTER_CODE_CANARY"
ERROR_CANARY   = "CR220_PRIVATE_EXCEPTION_CANARY"


def _messages(caplog: pytest.LogCaptureFixture, *logger_names: str) -> str:
    selected = set(logger_names)
    return "\n".join(record.getMessage() for record in caplog.records if record.name in selected)


@pytest.mark.asyncio
async def test_shell_command_is_executed_but_only_fingerprinted_in_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = (
        f'python -c "print(1)" --token={COMMAND_CANARY} '
        r"C:\private\operator-script.py"
    )
    executed: dict[str, object] = {}

    async def execute(command_args: list[str], timeout: int):
        executed["args"]    = command_args
        executed["timeout"] = timeout
        return 0, "ok", ""

    monkeypatch.setattr(shell_main, "_validate_command", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(shell_main, "_split_command", lambda _value: ["python", command])
    monkeypatch.setattr(shell_main, "_execute_command", execute)
    context = with_settings_reader(
        SimpleNamespace(
            request_id = "req-shell-cr220",
            secrets    = {"plugins": {"shell": {"timeout": 17}}},
        )
    )

    with caplog.at_level(logging.INFO, logger=shell_main.logger.name):
        response = await shell_main.handle(
            "shell",
            command,
            {"user_id": "CR220_PRIVATE_USER_ID"},
            context,
        )

    logged  = _messages(caplog, shell_main.logger.name)
    summary = summarize_sensitive(command)
    assert executed == {"args": ["python", command], "timeout": 17}
    assert "ok" in text_segments_text(response)
    assert command not in logged
    assert COMMAND_CANARY not in logged
    assert "operator-script.py" not in logged
    assert "CR220_PRIVATE_USER_ID" not in logged
    assert f"payload_length={summary.length}" in logged
    assert f"payload_bytes={summary.byte_length}" in logged
    assert f"payload_fingerprint={summary.fingerprint}" in logged
    assert "request_id=req-shell-cr220" in logged
    assert "status=started" in logged
    assert "status=succeeded" in logged


@pytest.mark.asyncio
async def test_shell_exception_body_stays_out_of_logs_and_public_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = f"echo {COMMAND_CANARY}"
    detail  = f"{ERROR_CANARY} C:\\private\\shell-error.txt"

    async def fail_execute(_command_args: list[str], _timeout: int):
        raise RuntimeError(detail)

    monkeypatch.setattr(shell_main, "_validate_command", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(shell_main, "_split_command", lambda _value: ["echo", COMMAND_CANARY])
    monkeypatch.setattr(shell_main, "_execute_command", fail_execute)
    context = with_settings_reader(
        SimpleNamespace(
            request_id = "req-shell-error",
            secrets    = {
                "plugins": {"shell": {"timeout": 3}},
                "private_exception_canary": ERROR_CANARY,
            },
        )
    )

    with caplog.at_level(logging.INFO, logger=shell_main.logger.name):
        response = await shell_main.handle("shell", command, {}, context)

    logged      = _messages(caplog, shell_main.logger.name)
    public_text = text_segments_text(response)
    assert "XQ-PLUGIN-UNEXPECTED" in public_text
    assert "req-shell-error" in public_text
    assert detail not in public_text
    assert ERROR_CANARY not in public_text
    assert ERROR_CANARY not in logged
    assert "shell-error.txt" not in logged
    assert "error_type=RuntimeError" in logged
    assert "status=error" in logged


@pytest.mark.asyncio
async def test_jupyter_code_is_executed_but_only_fingerprinted_in_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    credential_name = "to" + "ken"
    code            = (
        f'{credential_name} = "{CODE_CANARY}"\n'
        r'path = "C:\private\analysis.ipynb"'
        "\nprint(token)"
    )
    executed: dict[str, object] = {}

    class Manager:
        async def execute(self, value: str, *, timeout: float, audit_id: str):
            executed.update(value=value, timeout=timeout, audit_id=audit_id)
            return ExecutionResult(stdout="ok")

    monkeypatch.setattr(
        jupyter_main.JupyterKernelManager,
        "get_instance",
        lambda *_args: Manager(),
    )
    context = SimpleNamespace(
        data_dir         = tmp_path,
        current_user_id  = 1,
        current_group_id = None,
        request_id       = "req-jupyter-cr220",
    )

    with caplog.at_level(logging.INFO, logger=jupyter_main.logger.name):
        response = await jupyter_main._handle_execute(code, context)

    logged  = _messages(caplog, jupyter_main.logger.name)
    summary = summarize_sensitive(code)
    assert executed["value"] == code
    assert executed["timeout"] > 0
    assert isinstance(executed["audit_id"], str)
    assert "ok" in text_segments_text(response)
    assert code not in logged
    assert CODE_CANARY not in logged
    assert "analysis.ipynb" not in logged
    assert f"payload_length={summary.length}" in logged
    assert f"payload_bytes={summary.byte_length}" in logged
    assert f"payload_fingerprint={summary.fingerprint}" in logged
    assert "request_id=req-jupyter-cr220" in logged
    assert "status=started" in logged
    assert "status=succeeded" in logged


@pytest.mark.asyncio
async def test_jupyter_exception_body_stays_out_of_logs_and_public_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    code   = f'raise RuntimeError("{CODE_CANARY}")'
    detail = f"{ERROR_CANARY} C:\\private\\kernel.json"

    class Manager:
        async def execute(self, _value: str, *, timeout: float, audit_id: str):
            raise RuntimeError(detail)

    monkeypatch.setattr(
        jupyter_main.JupyterKernelManager,
        "get_instance",
        lambda *_args: Manager(),
    )
    context = SimpleNamespace(
        data_dir         = tmp_path,
        current_user_id  = 1,
        current_group_id = None,
        request_id       = "req-jupyter-error",
    )

    with caplog.at_level(logging.INFO, logger=jupyter_main.logger.name):
        response = await jupyter_main._handle_execute(code, context)

    logged      = _messages(caplog, jupyter_main.logger.name)
    public_text = text_segments_text(response)
    assert "执行失败，请稍后重试" in public_text
    assert detail not in public_text
    assert ERROR_CANARY not in public_text
    assert CODE_CANARY not in logged
    assert ERROR_CANARY not in logged
    assert "kernel.json" not in logged
    assert "error_type=RuntimeError" in logged
    assert "status=error" in logged


def test_shell_and_jupyter_ordinary_logger_ast_never_receives_raw_payloads() -> None:
    targets = (
        ROOT / "plugins" / "shell" / "main.py",
        ROOT / "plugins" / "jupyter" / "main.py",
        ROOT / "plugins" / "jupyter" / "jupyter_manager.py",
    )
    forbidden_names = {
        "args",
        "cmd_line",
        "code",
        "code_buffer",
        "command_text",
        "event",
        "exc",
        "IMPORT_ERROR",
        "report",
        "user_input",
    }
    violations: list[str] = []

    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if isinstance(call.func, ast.Attribute) and call.func.attr == "exception":
                violations.append(f"{path.name}:{call.lineno}: logger.exception")
                continue
            if any(keyword.arg == "exc_info" for keyword in call.keywords):
                violations.append(f"{path.name}:{call.lineno}: exc_info")

            is_log_call = (
                isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "logger"
            ) or (isinstance(call.func, ast.Name) and call.func.id == "log_method")
            if not is_log_call:
                continue
            referenced = {
                node.id
                for argument in (*call.args, *(keyword.value for keyword in call.keywords))
                for node in ast.walk(argument)
                if isinstance(node, ast.Name)
            }
            leaked_names = sorted(referenced & forbidden_names)
            if leaked_names:
                violations.append(f"{path.name}:{call.lineno}: raw names {','.join(leaked_names)}")

    assert violations == []


def test_shell_and_qingssh_broad_exception_handlers_never_format_exception_text() -> None:
    """宽泛异常出口只能记录类型或走公开错误协议，不能插值异常原文。"""

    targets = (
        ROOT / "plugins" / "shell" / "main.py",
        *(ROOT / "plugins" / "qingssh").glob("*.py"),
    )
    violations: list[str] = []

    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            if (
                not isinstance(handler.type, ast.Name)
                or handler.type.id != "Exception"
                or not handler.name
            ):
                continue
            body = ast.Module(body=handler.body, type_ignores=[])
            for formatted in (
                node for node in ast.walk(body) if isinstance(node, ast.FormattedValue)
            ):
                if isinstance(formatted.value, ast.Name) and formatted.value.id == handler.name:
                    violations.append(f"{path.name}:{formatted.lineno}: {handler.name}")

    assert violations == []
