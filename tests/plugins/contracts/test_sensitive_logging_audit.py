# 验证日志隐藏凭据与任意用户命令中的敏感内容。
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

COMMAND_CANARY       = "printf CR220_COMMAND_SECRET && token=CR220_TOKEN_SECRET"
OUTPUT_CANARY        = "CR220_REMOTE_RESPONSE_SECRET"
HOST_CANARY          = "cr220-private-host.internal"
PATH_CANARY          = "C:/private/CR220_PATH_SECRET/latest.log"
ERROR_CANARY         = "CR220_EXCEPTION_SECRET"
MALICIOUS_REQUEST_ID = "safe\nCR220_REQUEST_SECRET"
_FINGERPRINT_RE      = re.compile(r"hmac-sha256:[0-9a-f]{24}")


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
    command_seen: list[str]       = []

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
        request_id       = MALICIOUS_REQUEST_ID,
        send_action      = _send_action,
        current_user_id  = 1,
        current_group_id = None,
    )
    policy = SSHOutputPolicy(
        command_timeout_seconds  = 0,
        qq_send_interval_seconds = 0,
        qq_send_timeout_seconds  = 1,
    )
    session = _Session()
    job_key = (1, None)
    job_id  = uuid.uuid4().hex
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
                job_key    = job_key,
                job_id     = job_id,
                request_id = MALICIOUS_REQUEST_ID,
            )
        )
        qingssh_session_handlers._register_job(
            qingssh_session_handlers._CommandJob(
                key         = job_key,
                server_name = HOST_CANARY,
                job_id      = job_id,
                task        = task,
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
        request_id = "req-cr220-ssh-path",
        logger     = logging.getLogger("test.cr220.qingssh.manager"),
        secrets    = {"plugins": {"qingssh": {"password": ERROR_CANARY}}},
    )
    manager = SSHManager(tmp_path, context=context)
    manager.is_connected = lambda *_args: True  # type: ignore[method-assign]
    key                  = manager.build_connection_key("1", "None", "server")

    class _Client:
        def open_sftp(self):
            raise RuntimeError(ERROR_CANARY)

    manager.connections[key] = _Client()
    local_path               = str(tmp_path / "CR220_LOCAL_PATH_SECRET.png")

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

    rcon    = _Rcon()
    manager = minecraft.ConnectionManager()
    target  = DeliveryTarget("private", 7)
    await manager.replace_connection(
        minecraft.McConnection(
            host        = HOST_CANARY,
            port        = 25575,
            target      = target,
            rcon_client = rcon,
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
