from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from core.process_tree import ProcessResult
from core.sensitive_audit import summarize_sensitive
from plugins.jupyter import jupyter_manager as manager_module
from plugins.jupyter import main as jupyter_main
from plugins.jupyter.jupyter_manager import JupyterKernelManager
from plugins.shell import main as shell_main
from tests.helpers.assertions import text_segments_text
from tests.helpers.jupyter_test_support import (
    FakeKernel,
    FakeKernelClient,
    install_kernel_backend,
)
from tests.helpers.settings_snapshot import with_settings_reader

COMMAND_CANARY = "SHELL_TOKEN_CANARY"
CODE_CANARY    = "JUPYTER_CODE_CANARY"
ERROR_CANARY   = "PRIVATE_EXCEPTION_CANARY"


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
    run = AsyncMock(return_value=ProcessResult(0, "ok", ""))
    monkeypatch.setattr(shell_main, "run_process", run)
    context = with_settings_reader(
        SimpleNamespace(
            request_id = "req-shell",
            secrets    = {"plugins": {"shell": {"timeout": 17}}},
        )
    )

    with caplog.at_level(logging.INFO, logger=shell_main.logger.name):
        response = await shell_main.handle(
            "shell",
            command,
            {"user_id": "PRIVATE_USER_ID"},
            context,
        )

    logged  = _messages(caplog, shell_main.logger.name)
    summary = summarize_sensitive(command)
    argv    = run.await_args.args[0]
    assert argv[0] == "python"
    assert f"--token={COMMAND_CANARY}" in argv
    assert run.await_args.kwargs["timeout"] == 17
    assert "ok" in text_segments_text(response)
    assert command not in logged
    assert COMMAND_CANARY not in logged
    assert "operator-script.py" not in logged
    assert "PRIVATE_USER_ID" not in logged
    assert f"payload_length={summary.length}" in logged
    assert f"payload_bytes={summary.byte_length}" in logged
    assert f"payload_fingerprint={summary.fingerprint}" in logged
    assert "request_id=req-shell" in logged
    assert "status=started" in logged
    assert "status=succeeded" in logged


@pytest.mark.asyncio
async def test_shell_exception_body_stays_out_of_logs_and_public_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = f"echo {COMMAND_CANARY}"
    detail  = f"{ERROR_CANARY} C:\\private\\shell-error.txt"

    monkeypatch.setattr(shell_main, "run_process", AsyncMock(side_effect=RuntimeError(detail)))
    context = with_settings_reader(
        SimpleNamespace(
            request_id = "req-shell-error",
            secrets    = {
                "plugins"                 : {"shell": {"timeout": 3}},
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


@pytest_asyncio.fixture
async def kernel_client(monkeypatch: pytest.MonkeyPatch):
    """依赖可用，内核由替身承担；用例结束时关掉全部内核。"""

    client = FakeKernelClient()
    install_kernel_backend(monkeypatch, lambda **_kwargs: FakeKernel(client))
    await JupyterKernelManager.shutdown_all_async()
    yield client
    await JupyterKernelManager.shutdown_all_async()


@pytest.mark.asyncio
async def test_jupyter_code_is_executed_but_only_fingerprinted_in_logs(
    kernel_client: FakeKernelClient,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    credential_name = "to" + "ken"
    code            = (
        f'{credential_name} = "{CODE_CANARY}"\n'
        r'path = "C:\private\analysis.ipynb"'
        "\nprint(token)"
    )
    kernel_client.responder = lambda _code: [
        {"msg_type": "stream", "content": {"name": "stdout", "text": "ok"}}
    ]
    context = SimpleNamespace(
        data_dir         = tmp_path,
        current_user_id  = 1,
        current_group_id = None,
        request_id       = "req-jupyter",
    )

    with caplog.at_level(logging.INFO, logger=jupyter_main.logger.name):
        response = await jupyter_main._handle_execute(code, context)

    logged  = _messages(caplog, jupyter_main.logger.name)
    summary = summarize_sensitive(code)
    assert kernel_client.executed[-1] == code
    assert "ok" in text_segments_text(response)
    assert code not in logged
    assert CODE_CANARY not in logged
    assert "analysis.ipynb" not in logged
    assert f"payload_length={summary.length}" in logged
    assert f"payload_bytes={summary.byte_length}" in logged
    assert f"payload_fingerprint={summary.fingerprint}" in logged
    assert "request_id=req-jupyter" in logged
    assert "status=started" in logged
    assert "status=succeeded" in logged


@pytest.mark.asyncio
async def test_jupyter_exception_body_stays_out_of_logs_and_public_response(
    kernel_client: FakeKernelClient,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    code                      = f'raise RuntimeError("{CODE_CANARY}")'
    detail                    = f"{ERROR_CANARY} C:\\private\\kernel.json"
    kernel_client.fail_submit = detail
    context                   = SimpleNamespace(
        data_dir         = tmp_path,
        current_user_id  = 1,
        current_group_id = None,
        request_id       = "req-jupyter-error",
    )

    with caplog.at_level(logging.INFO, logger=jupyter_main.logger.name):
        response = await jupyter_main._handle_execute(code, context)

    logged      = _messages(caplog, jupyter_main.logger.name, manager_module.logger.name)
    public_text = text_segments_text(response)
    assert "执行失败，请稍后重试" in public_text
    assert detail not in public_text
    assert ERROR_CANARY not in public_text
    assert CODE_CANARY not in logged
    assert ERROR_CANARY not in logged
    assert "kernel.json" not in logged
    assert "error_type=RuntimeError" in logged
    assert "status=error" in logged
