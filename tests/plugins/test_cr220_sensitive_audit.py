from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.sensitive_audit import summarize_sensitive
from plugins.minecraft import main as minecraft
from plugins.minecraft.log_monitor import LogMonitor
from plugins.minecraft.rcon import RconCommandResult
from plugins.qingssh.config import SessionKeys
from plugins.qingssh.output_relay import SSHOutputPolicy
from plugins.qingssh.session_handlers import _run_background_command
from plugins.qingssh.ssh_manager import SSHManager

COMMAND_CANARY = "printf CR220_COMMAND_SECRET && token=CR220_TOKEN_SECRET"
OUTPUT_CANARY = "CR220_REMOTE_RESPONSE_SECRET"
HOST_CANARY = "cr220-private-host.internal"
PATH_CANARY = "C:/private/CR220_PATH_SECRET/latest.log"
ERROR_CANARY = "CR220_EXCEPTION_SECRET"
MALICIOUS_REQUEST_ID = "safe\nCR220_REQUEST_SECRET"
_FINGERPRINT_RE = re.compile(r"hmac-sha256:[0-9a-f]{24}")


class _Session:
    def __init__(self) -> None:
        self.values = {
            SessionKeys.STATE: "executing",
            SessionKeys.CURRENT_TASK: "running",
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


def _action_texts(actions: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for action in actions:
        for segment in action.get("params", {}).get("message", []):
            if segment.get("type") == "text":
                texts.append(str(segment.get("data", {}).get("text", "")))
    return "".join(texts)


@pytest.mark.asyncio
async def test_qingssh_command_and_response_stay_admin_visible_but_not_in_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    actions: list[dict[str, Any]] = []
    command_seen: list[str] = []

    class _Manager:
        data_dir = tmp_path

        async def execute_command_stream(
            self,
            _user_id: str,
            _group_id: str,
            _server: str,
            command: str,
            callback: Any,
            *,
            timeout: float,
        ) -> int:
            command_seen.append(command)
            assert timeout == 0
            await callback(OUTPUT_CANARY)
            return 0

    async def _send_action(action: dict[str, Any]) -> bool:
        actions.append(action)
        return True

    context = SimpleNamespace(
        request_id=MALICIOUS_REQUEST_ID,
        send_action=_send_action,
        current_user_id=1,
        current_group_id=None,
    )
    policy = SSHOutputPolicy(
        command_timeout_seconds=0,
        qq_send_interval_seconds=0,
        qq_send_timeout_seconds=1,
    )

    with caplog.at_level(logging.INFO):
        await _run_background_command(
            context,
            _Session(),
            _Manager(),
            HOST_CANARY,
            COMMAND_CANARY,
            "1",
            "None",
            1,
            None,
            policy,
        )

    assert command_seen == [COMMAND_CANARY]
    assert OUTPUT_CANARY in _action_texts(actions)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        COMMAND_CANARY,
        OUTPUT_CANARY,
        HOST_CANARY,
        "CR220_REQUEST_SECRET",
    ):
        assert secret not in logs
    assert summarize_sensitive(COMMAND_CANARY).fingerprint in logs
    assert f"payload_length={len(COMMAND_CANARY)}" in logs
    assert "request_id=-" in logs
    assert _FINGERPRINT_RE.search(logs)
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_qingssh_path_and_exception_are_fingerprinted_not_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = SimpleNamespace(
        request_id="req-cr220-ssh-path",
        logger=logging.getLogger("test.cr220.qingssh.manager"),
    )
    manager = SSHManager(tmp_path, context=context)
    manager.is_connected = lambda *_args: True  # type: ignore[method-assign]
    key = manager._build_connection_key("1", "None", "server")

    class _Client:
        def open_sftp(self):
            raise RuntimeError(ERROR_CANARY)

    manager.connections[key] = _Client()
    local_path = str(tmp_path / "CR220_LOCAL_PATH_SECRET.png")

    with caplog.at_level(logging.ERROR):
        success, admin_message = await manager.download_file(
            "1",
            "None",
            "server",
            PATH_CANARY,
            local_path,
        )

    assert success is False
    assert ERROR_CANARY in admin_message
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert ERROR_CANARY not in logs
    assert PATH_CANARY not in logs
    assert local_path not in logs
    assert summarize_sensitive("\0".join((PATH_CANARY, local_path))).fingerprint in logs
    assert "error_type=RuntimeError" in logs
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_minecraft_command_response_and_host_stay_out_of_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        async def command(self, command: str) -> RconCommandResult:
            self.commands.append(command)
            return RconCommandResult(success=True, response=OUTPUT_CANARY)

    rcon = _Rcon()
    manager = minecraft.ConnectionManager()
    manager.add_connection(
        minecraft.McConnection(
            host=HOST_CANARY,
            port=25575,
            password="CR220_RCON_PASSWORD_SECRET",
            log_file=PATH_CANARY,
            target_type="private",
            target_id=7,
            rcon_client=rcon,
        )
    )
    monkeypatch.setattr(minecraft, "_manager", manager)
    context = SimpleNamespace(request_id=MALICIOUS_REQUEST_ID)

    with caplog.at_level(logging.INFO):
        response = await minecraft._handle_mc_message(
            COMMAND_CANARY,
            {"user_id": 7, "message_type": "private"},
            context,
        )

    assert rcon.commands == [COMMAND_CANARY]
    assert OUTPUT_CANARY in str(response)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        COMMAND_CANARY,
        OUTPUT_CANARY,
        HOST_CANARY,
        PATH_CANARY,
        "CR220_RCON_PASSWORD_SECRET",
        "CR220_REQUEST_SECRET",
    ):
        assert secret not in logs
    assert summarize_sensitive(COMMAND_CANARY).fingerprint in logs
    assert f"payload_length={len(COMMAND_CANARY)}" in logs
    assert "request_id=-" in logs
    assert all(record.exc_info is None for record in caplog.records)


def test_minecraft_log_path_is_fingerprinted_not_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing_path = tmp_path / "CR220_PATH_SECRET" / "latest.log"

    with caplog.at_level(logging.WARNING):
        assert LogMonitor(str(missing_path)).initialize() is False

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert str(missing_path) not in logs
    assert "CR220_PATH_SECRET" not in logs
    assert summarize_sensitive(str(missing_path)).fingerprint in logs
    assert all(record.exc_info is None for record in caplog.records)


def _logger_sink_name(call: ast.Call) -> str | None:
    function = call.func
    if not isinstance(function, ast.Attribute):
        return None
    if function.attr == "_log":
        return "_log"
    if function.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
        return None
    receiver = function.value
    if isinstance(receiver, ast.Name) and receiver.id == "logger":
        return function.attr
    if isinstance(receiver, ast.Attribute) and receiver.attr == "logger":
        return function.attr
    return None


class _UnsafeLogArgumentVisitor(ast.NodeVisitor):
    _FORBIDDEN_NAMES = {
        "archive_path",
        "cmd",
        "command",
        "e",
        "error",
        "exc",
        "host",
        "key",
        "local_path",
        "log_file",
        "log_path",
        "password",
        "remote_dir",
        "remote_path",
        "response",
        "server_name",
    }
    _SAFE_CALLS = {"audit_error_type", "audit_request_id", "audit_id"}

    def __init__(self) -> None:
        self.unsafe: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self._SAFE_CALLS:
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self._FORBIDDEN_NAMES:
            self.unsafe.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self._FORBIDDEN_NAMES:
            self.unsafe.add(node.attr)
        self.generic_visit(node)


def test_qingssh_and_minecraft_logger_calls_reject_sensitive_ast_arguments() -> None:
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for plugin_name in ("qingssh", "minecraft"):
        for path in sorted((root / "plugins" / plugin_name).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                sink = _logger_sink_name(node)
                if sink is None:
                    continue
                if sink == "exception":
                    violations.append(f"{path.name}:{node.lineno}: logger.exception")
                if any(keyword.arg == "exc_info" for keyword in node.keywords):
                    violations.append(f"{path.name}:{node.lineno}: exc_info")
                visitor = _UnsafeLogArgumentVisitor()
                for argument in node.args:
                    visitor.visit(argument)
                if visitor.unsafe:
                    violations.append(
                        f"{path.name}:{node.lineno}: raw={','.join(sorted(visitor.unsafe))}"
                    )

    assert violations == []
