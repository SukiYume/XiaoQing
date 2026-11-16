"""QingSSH 测试共享导入和私有 helper。"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any, cast

from core.session import SessionManager
from plugins.qingssh import main as qingssh_main
from plugins.qingssh import session_handlers as ssh_session_handlers
from plugins.qingssh import ssh_manager as ssh_manager_module
from plugins.qingssh.config import EXIT_CODE_TIMEOUT, SessionKeys
from plugins.qingssh.validators import (
    validate_command,
    validate_hostname,
    validate_port,
    validate_server_name,
    validate_username,
)
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def command_parent(command: str, context: Any, manager: Any):
    """构造只负责提交一条后台命令的父事务回调。"""

    async def run(working: Any) -> None:
        await ssh_session_handlers._handle_connected_session(
            command,
            context,
            working,
            manager,
        )

    return run


class SessionStub:
    def __init__(self, data=None):
        self.data        = data or {}
        self.plugin_name = "qingssh"

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class ManagerStub:
    data_dir = ROOT

    def __init__(self):
        self._stop = False
        self._done = asyncio.Event()

    def is_connected(self, user_id, group_id, server_name):
        return True

    async def stop_command(self, user_id, group_id, server_name):
        self._stop = True
        return ssh_manager_module.CommandTerminationResult(
            found            = True,
            local_cleaned    = True,
            remote_confirmed = True,
        )

    async def execute_command_stream(self, *args, **kwargs):
        await self._done.wait()
        return 0


class DisconnectManagerStub:
    def __init__(self):
        self.calls = []

    async def stop_command(self, _user_id, _group_id, _server_name):
        return ssh_manager_module.CommandTerminationResult(
            found            = False,
            local_cleaned    = True,
            remote_confirmed = False,
        )

    def disconnect(self, user_id, group_id, server_name):
        self.calls.append((user_id, group_id, server_name))
        return server_name == "other-srv"


_NO_REPLACEMENT = object()


class TransactionalContext:
    """Small deterministic model of the framework's per-session transaction."""

    def __init__(self, session: SessionStub) -> None:
        self.current_user_id        = 10001
        self.current_group_id       = 50001
        self.config: dict[str, Any] = {
            "plugins": {
                "qingssh": {
                    "qq_send_interval_seconds": 0,
                    "qq_send_timeout_seconds" : 0.2,
                }
            }
        }
        self.request_id                    = "qingssh-transaction-test"
        self.session: SessionStub | None   = session
        self.actions: list[dict[str, Any]] = []
        self._lock                         = asyncio.Lock()

    def get_settings_snapshot(self):
        from tests.helpers.settings_snapshot import settings_snapshot

        return settings_snapshot(config=self.config)

    async def send_action(self, action: dict[str, Any]) -> None:
        self.actions.append(action)

    async def update_session(self, callback: Any) -> Any:
        async with self._lock:
            if self.session is None:
                return None
            return callback(self.session)

    async def run_parent(
        self,
        callback: Any,
        *,
        replacement: object = _NO_REPLACEMENT,
    ) -> Any:
        async with self._lock:
            if self.session is None:
                raise AssertionError("test session is missing")
            working      = copy.deepcopy(self.session)
            result       = await callback(working)
            self.session = working if replacement is _NO_REPLACEMENT else cast(Any, replacement)
            return result


class CommandGateManager:
    def __init__(self, data_dir: Path, *, output: str = "") -> None:
        self.data_dir                                = data_dir
        self.output                                  = output
        self.calls: list[str]                        = []
        self.disconnects: list[tuple[str, str, str]] = []
        self.close_operations: list[str]             = []
        self.started                                 = asyncio.Event()
        self.release                                 = asyncio.Event()

    def is_connected(self, *_args: Any) -> bool:
        return True

    def disconnect(self, user_id: str, group_id: str, server_name: str) -> bool:
        self.close_operations.append("disconnect")
        self.disconnects.append((user_id, group_id, server_name))
        return True

    async def stop_command(self, _user_id: str, _group_id: str, _server_name: str):
        self.close_operations.append("stop")
        return ssh_manager_module.CommandTerminationResult(
            found            = True,
            local_cleaned    = True,
            remote_confirmed = True,
        )

    async def execute_command_stream(
        self,
        _user_id: str,
        _group_id: str,
        _server_name: str,
        command: str,
        callback: Any,
        *,
        timeout: float,
    ) -> int:
        assert timeout > 0
        self.calls.append(command)
        self.started.set()
        await self.release.wait()
        if self.output:
            await callback(self.output)
        return 0


def connected_session() -> SessionStub:
    return SessionStub(
        {
            SessionKeys.STATE        : "connected",
            SessionKeys.SERVER_NAME  : "srv1",
            SessionKeys.COMMAND_COUNT: 0,
            SessionKeys.CWD          : "/initial",
            SessionKeys.ENV_VARS     : {},
            SessionKeys.HISTORY      : [],
        }
    )


async def wait_for(predicate: Any, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


class _FakeJumpClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.closed       = False
        self._jump_client = _FakeJumpClient()

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self):
        self.closed = False
        self.sent   = []

    def send_ready(self):
        return True

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


class StreamChannel:
    """paramiko 通道替身：按脚本给出输出；``exit_status`` 为 ``None`` 表示命令一直不结束。"""

    active = True

    def __init__(self, chunks: tuple[bytes, ...] = (), *, exit_status: int | None = None) -> None:
        self.chunks              = list(chunks)
        self.exit_status         = exit_status
        self.executed: list[str] = []
        self.closed              = False

    def set_combine_stderr(self, _combine: bool) -> None:
        return None

    def exec_command(self, command: str) -> None:
        self.executed.append(command)

    def exit_status_ready(self) -> bool:
        return self.exit_status is not None and not self.chunks

    def recv_ready(self) -> bool:
        return bool(self.chunks)

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        self.closed = True


class StreamClient:
    """paramiko 客户端替身：同时充当传输层，终止信号命令记录在 ``signals``。

    ``on_signal`` 在每次发出终止信号时调用，测试可借它卡住“远端清理进行中”。
    """

    def __init__(self, channel: StreamChannel, on_signal: Any = None) -> None:
        self._channel           = channel
        self._on_signal         = on_signal
        self.signals: list[str] = []

    def get_transport(self) -> StreamClient:
        return self

    def is_active(self) -> bool:
        return True

    def open_session(self) -> StreamChannel:
        return self._channel

    def exec_command(self, command: str) -> None:
        self.signals.append(command)
        if self._on_signal is not None:
            self._on_signal()

    def close(self) -> None:
        return None


__all__ = (
    "Any",
    "EXIT_CODE_TIMEOUT",
    "Path",
    "ROOT",
    "SessionKeys",
    "SessionManager",
    "StreamChannel",
    "StreamClient",
    "CommandGateManager",
    "DisconnectManagerStub",
    "FakeChannel",
    "FakeClient",
    "_FakeJumpClient",
    "ManagerStub",
    "_NO_REPLACEMENT",
    "SessionStub",
    "TransactionalContext",
    "command_parent",
    "connected_session",
    "wait_for",
    "asyncio",
    "cast",
    "copy",
    "qingssh_main",
    "ssh_manager_module",
    "ssh_session_handlers",
    "validate_command",
    "validate_hostname",
    "validate_port",
    "validate_server_name",
    "validate_username",
)
