from __future__ import annotations

import ast
import asyncio
import logging
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from core.interfaces import DeliveryTarget
from core.sensitive_audit import summarize_sensitive
from plugins.minecraft import main as minecraft
from plugins.minecraft.log_monitor import LogMonitor
from plugins.minecraft.rcon import RconCommandResult
from plugins.qingssh import session_handlers as qingssh_session_handlers
from plugins.qingssh.config import SessionKeys
from plugins.qingssh.output_relay import SSHOutputPolicy
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
        }
        self.plugin_name = "qingssh"

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
    session = _Session()
    job_key = (1, None)
    job_id = uuid.uuid4().hex
    session.set(SessionKeys.SERVER_NAME, HOST_CANARY)
    session.set(SessionKeys.CURRENT_TASK, job_id)

    async def update_session(callback: Any) -> Any:
        return callback(session)

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            qingssh_session_handlers._run_background_command(
                update_session,
                context.send_action,
                _Manager(),
                HOST_CANARY,
                COMMAND_CANARY,
                "1",
                "None",
                1,
                None,
                policy,
                job_key=job_key,
                job_id=job_id,
                request_id=MALICIOUS_REQUEST_ID,
            )
        )
        qingssh_session_handlers._register_job(
            qingssh_session_handlers._CommandJob(
                key=job_key,
                server_name=HOST_CANARY,
                job_id=job_id,
                task=task,
            )
        )
        await task

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
        secrets={"plugins": {"qingssh": {"password": ERROR_CANARY}}},
    )
    manager = SSHManager(tmp_path, context=context)
    manager.is_connected = lambda *_args: True  # type: ignore[method-assign]
    key = manager.build_connection_key("1", "None", "server")

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
    assert ERROR_CANARY not in admin_message
    assert "XQ-PLUGIN-UNEXPECTED" in admin_message
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
    target = DeliveryTarget("private", 7)
    await manager.replace_connection(
        minecraft.McConnection(
            host=HOST_CANARY,
            port=25575,
            target=target,
            rcon_client=rcon,
        )
    )
    monkeypatch.setattr(minecraft, "_manager", manager)
    context = SimpleNamespace(request_id=MALICIOUS_REQUEST_ID)

    with caplog.at_level(logging.INFO):
        response = await minecraft._handle_mc_command(
            COMMAND_CANARY,
            target,
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


def _logger_sink_name(call: ast.Call, logger_aliases: set[str]) -> str | None:
    function = call.func
    if not isinstance(function, ast.Attribute):
        return None
    if function.attr == "_log":
        return "_log"
    if function.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
        return None
    receiver = function.value
    if isinstance(receiver, ast.Name) and receiver.id in logger_aliases:
        return function.attr
    if isinstance(receiver, ast.Attribute) and receiver.attr == "logger":
        return function.attr
    return None


class _UnsafeLogArgumentVisitor(ast.NodeVisitor):
    _FORBIDDEN_NAMES: ClassVar[set[str]] = {
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
    _SAFE_CALLS: ClassVar[set[str]] = {
        "audit_error_type",
        "audit_request_id",
        "audit_id",
        "summarize_sensitive",
    }
    _SAFE_CALL_ATTRIBUTES: ClassVar[set[str]] = {
        "_classify_error",
        "connect",
        "execute_command_stream",
        "finish",
    }
    _SAFE_METADATA_ATTRIBUTES: ClassVar[set[str]] = {
        "actions_attempted",
        "byte_length",
        "delivery_errors",
        "error_kind",
        "fingerprint",
        "kind",
        "length",
        "qq_truncated",
        "total_bytes",
        "total_chars",
    }

    def __init__(self, forbidden_names: set[str] | None = None) -> None:
        self.forbidden_names = forbidden_names or set(self._FORBIDDEN_NAMES)
        self.unsafe: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in self._SAFE_CALLS:
            return
        if isinstance(node.func, ast.Attribute) and node.func.attr in self._SAFE_CALL_ATTRIBUTES:
            return
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # Comparisons collapse values to a boolean and cannot disclose the input.
        return

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.forbidden_names:
            self.unsafe.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self._SAFE_METADATA_ATTRIBUTES:
            return
        if node.attr in self._FORBIDDEN_NAMES:
            self.unsafe.add(node.attr)
        self.generic_visit(node)


def _assigned_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for element in target.elts for name in _assigned_names(element)}
    return set()


def _sensitive_aliases(tree: ast.AST) -> set[str]:
    aliases = set(_UnsafeLogArgumentVisitor._FORBIDDEN_NAMES)
    assignments: list[tuple[set[str], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = {name for target in node.targets for name in _assigned_names(target)}
            assignments.append((targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append((_assigned_names(node.target), node.value))

    changed = True
    while changed:
        changed = False
        for targets, value in assignments:
            visitor = _UnsafeLogArgumentVisitor(aliases)
            visitor.visit(value)
            if visitor.unsafe and not targets <= aliases:
                aliases.update(targets)
                changed = True
    return aliases


def _logger_aliases(tree: ast.AST) -> set[str]:
    aliases = {"logger"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            is_logger = (
                isinstance(value, ast.Name)
                and value.id in aliases
                or isinstance(value, ast.Attribute)
                and value.attr == "logger"
            )
            if not is_logger:
                continue
            targets = (
                {name for target in node.targets for name in _assigned_names(target)}
                if isinstance(node, ast.Assign)
                else _assigned_names(node.target)
            )
            if not targets <= aliases:
                aliases.update(targets)
                changed = True
    return aliases


def _logging_violations(tree: ast.AST, label: str) -> list[str]:
    violations: list[str] = []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    cache: dict[int, tuple[set[str], set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        scope: ast.AST = node
        while scope in parents:
            scope = parents[scope]
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                break
        scope_key = id(scope)
        if scope_key not in cache:
            cache[scope_key] = (_sensitive_aliases(scope), _logger_aliases(scope))
        sensitive_aliases, logger_aliases = cache[scope_key]
        sink = _logger_sink_name(node, logger_aliases)
        if sink is None:
            continue
        if sink == "exception":
            violations.append(f"{label}:{node.lineno}: logger.exception")
        if any(keyword.arg == "exc_info" for keyword in node.keywords):
            violations.append(f"{label}:{node.lineno}: exc_info")
        visitor = _UnsafeLogArgumentVisitor(sensitive_aliases)
        for argument in node.args:
            visitor.visit(argument)
        if visitor.unsafe:
            violations.append(f"{label}:{node.lineno}: raw={','.join(sorted(visitor.unsafe))}")
    return violations


def test_qingssh_and_minecraft_logger_calls_reject_sensitive_ast_arguments() -> None:
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for plugin_name in ("qingssh", "minecraft"):
        plugin_root = root / "plugins" / plugin_name
        for path in sorted(plugin_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(_logging_violations(tree, path.relative_to(plugin_root).as_posix()))

    assert violations == []


def test_sensitive_logging_gate_follows_value_and_logger_aliases() -> None:
    tree = ast.parse(
        """
def leak(password):
    copied = password
    audit_log = logger
    audit_log.info("credential=%s", copied)
"""
    )

    violations = _logging_violations(tree, "nested/module.py")

    assert len(violations) == 1
    assert "raw=copied" in violations[0]
