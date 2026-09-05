from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.qingssh import session_handlers
from plugins.qingssh import ssh_manager as ssh_manager_module
from plugins.qingssh.config import SessionKeys
from plugins.qingssh.output_relay import SSHOutputPolicy, SSHOutputRelay
from tests.helpers.settings_snapshot import with_settings_reader


class _Session:
    def __init__(self) -> None:
        self.data = {
            SessionKeys.STATE: "executing",
        }
        self.plugin_name = "qingssh"

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


def _start_background_job(
    *,
    session: _Session,
    manager: Any,
    send_action: Any,
    command: str,
    policy: SSHOutputPolicy,
    server_name: str = "srv",
    is_cd: bool      = False,
) -> asyncio.Task[None]:
    key    = (1, 2)
    job_id = uuid.uuid4().hex
    session.set(SessionKeys.SERVER_NAME, server_name)
    session.set(SessionKeys.STATE, "executing")
    session.set(SessionKeys.CURRENT_TASK, job_id)

    async def update_session(callback: Any) -> Any:
        return callback(session)

    task = asyncio.create_task(
        session_handlers._run_background_command(
            update_session,
            send_action,
            manager,
            server_name,
            command,
            "1",
            "2",
            1,
            2,
            policy,
            job_key = key,
            job_id  = job_id,
            is_cd   = is_cd,
        )
    )
    session_handlers._register_job(
        session_handlers._CommandJob(
            key         = key,
            server_name = server_name,
            job_id      = job_id,
            task        = task,
        )
    )
    return task


def _action_texts(actions: list[dict[str, Any]]) -> list[str]:
    return [str(action["params"]["message"][0]["data"]["text"]) for action in actions]


def _fast_policy(**overrides: Any) -> SSHOutputPolicy:
    values = {
        "command_timeout_seconds": 0.0,
        "qq_max_actions": 6,
        "qq_max_text_chars": 10_000,
        "qq_max_message_chars": 1_800,
        "qq_head_chars": 6_000,
        "qq_tail_chars": 2_000,
        "qq_send_interval_seconds": 0.0,
        "qq_send_timeout_seconds": 0.2,
        "archive_max_bytes": 8 * 1024 * 1024,
        "archive_tail_bytes": 64 * 1024,
    }
    values.update(overrides)
    return SSHOutputPolicy(**values)


@pytest.mark.asyncio
async def test_relay_strips_terminal_controls_only_from_user_projection(tmp_path: Path) -> None:
    sent: list[str] = []

    async def send_text(value: str) -> None:
        sent.append(value)

    relay = SSHOutputRelay(
        output_dir = tmp_path,
        policy     = _fast_policy(),
        send_text  = send_text,
    )
    await relay.feed("\x1b[31mvisible\x1b[0m\x00text")
    summary = await relay.finish("✅ done")

    projected = "".join(sent)
    assert "visible" in projected and "text" in projected
    assert "\x1b" not in projected and "\x00" not in projected
    assert summary.total_chars == len("\x1b[31mvisible\x1b[0m\x00text")


def test_policy_reads_plugin_namespace_and_allows_explicit_unlimited_command() -> None:
    context = with_settings_reader(
        SimpleNamespace(
            config={
                "plugins": {
                    "qingssh": {
                        "command_timeout_seconds": 0,
                        "qq_max_actions": 4,
                        "qq_max_text_chars": 4096,
                        "qq_max_message_chars": 1024,
                        "qq_head_chars": 2048,
                        "qq_tail_chars": 512,
                        "qq_send_interval_seconds": 0,
                        "qq_send_timeout_seconds": 2,
                        "archive_max_bytes": 2 * 1024 * 1024,
                        "archive_tail_bytes": 64 * 1024,
                    }
                }
            }
        )
    )

    policy = SSHOutputPolicy.from_context(context)

    assert policy.command_timeout_seconds == 0
    assert policy.qq_max_actions == 4
    assert policy.qq_head_chars == 2048


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("qq_max_actions", 1),
        ("qq_max_message_chars", 3001),
        ("qq_send_timeout_seconds", 0),
        ("archive_max_bytes", 1024),
        ("command_timeout_seconds", -1),
    ],
)
def test_policy_rejects_unsafe_or_nonfunctional_bounds(name: str, value: Any) -> None:
    context = with_settings_reader(SimpleNamespace(config={"plugins": {"qingssh": {name: value}}}))

    with pytest.raises(ValueError, match=name):
        SSHOutputPolicy.from_context(context)


@pytest.mark.asyncio
async def test_normal_output_is_complete_and_creates_no_archive(tmp_path: Path) -> None:
    actions: list[dict[str, Any]]         = []
    command_seen: list[tuple[str, float]] = []

    class Manager:
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
            command_seen.append((command, timeout))
            await callback("hello\n")
            await callback("world\n")
            return 0

    async def send_action(action: dict[str, Any]) -> None:
        actions.append(action)

    context = SimpleNamespace(send_action=send_action)
    session = _Session()
    command = "printf '%s\\n' 'hello world' && uname -a"

    await _start_background_job(
        session     = session,
        manager     = Manager(),
        send_action = context.send_action,
        command     = command,
        policy      = _fast_policy(),
    )

    texts = _action_texts(actions)
    assert command_seen == [(command, 0.0)]
    assert "".join(texts).startswith("hello\nworld\n")
    assert "✅ 命令执行完毕" in texts[-1]
    assert not (tmp_path / "command_outputs").exists()
    assert session.get(SessionKeys.STATE) == "connected"
    assert session.get(SessionKeys.CURRENT_TASK) is None


@pytest.mark.asyncio
async def test_archive_filesystem_operations_run_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = SSHOutputRelay(
        output_dir = tmp_path,
        policy     = _fast_policy(),
        send_text  = lambda _text: asyncio.sleep(0),
    )
    event_loop_thread                        = threading.get_ident()
    operation_threads: list[tuple[str, int]] = []

    for method_name in ("_open_archive", "_archive", "_commit_archive"):
        original = getattr(relay, method_name)

        def tracked(*args: Any, _name: str = method_name, _original: Any = original) -> Any:
            operation_threads.append((_name, threading.get_ident()))
            return _original(*args)

        monkeypatch.setattr(relay, method_name, tracked)

    await relay.feed("x" * 20_000)
    summary = await relay.finish("✅ done")

    assert summary.archive_path is not None
    assert {name for name, _thread_id in operation_threads} == {
        "_open_archive",
        "_archive",
        "_commit_archive",
    }
    assert all(thread_id != event_loop_thread for _name, thread_id in operation_threads)


@pytest.mark.asyncio
async def test_multi_megabyte_output_has_bounded_qq_projection_and_full_archive(
    tmp_path: Path,
) -> None:
    actions: list[dict[str, Any]] = []
    payload                       = "BEGIN\n" + ("x" * (4 * 1024 * 1024)) + "\nEND\n"

    class Manager:
        data_dir = tmp_path

        async def execute_command_stream(self, *args: Any, timeout: float) -> int:
            callback = args[4]
            assert args[3] == "yes x | head -c 4194304"
            assert timeout == 0
            for offset in range(0, len(payload), 4096):
                await callback(payload[offset : offset + 4096])
            return 0

    async def send_action(action: dict[str, Any]) -> None:
        actions.append(action)

    await _start_background_job(
        session     = _Session(),
        manager     = Manager(),
        send_action = send_action,
        command     = "yes x | head -c 4194304",
        policy      = _fast_policy(),
    )

    texts = _action_texts(actions)
    assert len(texts) <= 6
    assert sum(map(len, texts)) <= 10_000
    assert all(len(message) <= 1_800 for message in texts)
    assert texts[0].startswith("BEGIN\n")
    assert "END\n" in texts[-1]
    assert "QQ 输出已截断" in texts[-1]
    archives = list((tmp_path / "command_outputs").glob("ssh-output-*.txt"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == payload
    assert archives[0].name in texts[-1]
    assert str(archives[0].resolve()) not in texts[-1]
    assert not list((tmp_path / "command_outputs").glob("*.tmp"))


@pytest.mark.asyncio
async def test_archive_budget_preserves_head_and_tail_without_exceeding_cap(
    tmp_path: Path,
) -> None:
    sent: list[str] = []
    policy          = _fast_policy(
        archive_max_bytes  = 1024 * 1024,
        archive_tail_bytes = 64 * 1024,
    )
    relay = SSHOutputRelay(
        output_dir = tmp_path,
        policy     = policy,
        send_text  = lambda text: _append_async(sent, text),
    )
    await relay.feed("BEGIN\n" + ("z" * (2 * 1024 * 1024)) + "\nEND\n")

    summary = await relay.finish("✅ done")

    assert summary.archive_truncated is True
    assert summary.archive_path is not None
    archive = summary.archive_path.read_bytes()
    assert len(archive) <= policy.archive_max_bytes
    assert archive.startswith(b"BEGIN\n")
    assert archive.endswith(b"\nEND\n")
    assert b"middle omitted" in archive


async def _append_async(items: list[str], value: str) -> None:
    items.append(value)


@pytest.mark.asyncio
async def test_hung_qq_delivery_does_not_backpressure_remote_drain(tmp_path: Path) -> None:
    send_started = 0
    never        = asyncio.Event()

    async def hung_send(_text: str) -> None:
        nonlocal send_started
        send_started += 1
        await never.wait()

    policy = _fast_policy(qq_send_timeout_seconds=0.01)
    relay = SSHOutputRelay(output_dir=tmp_path, policy=policy, send_text=hung_send)
    payload = "q" * (4 * 1024 * 1024)

    for offset in range(0, len(payload), 4096):
        await relay.feed(payload[offset : offset + 4096])
    summary = await asyncio.wait_for(relay.finish("✅ done"), timeout=1)

    assert 1 <= send_started <= policy.qq_max_actions
    assert summary.actions_attempted <= policy.qq_max_actions
    assert summary.text_chars_attempted <= policy.qq_max_text_chars
    assert summary.delivery_errors == summary.actions_attempted


@pytest.mark.asyncio
async def test_cancelled_finish_removes_unreported_archive_and_sender(tmp_path: Path) -> None:
    send_started = asyncio.Event()
    never        = asyncio.Event()

    async def hung_send(_text: str) -> None:
        send_started.set()
        await never.wait()

    relay = SSHOutputRelay(
        output_dir=tmp_path,
        policy=_fast_policy(qq_send_timeout_seconds=10),
        send_text=hung_send,
    )
    await relay.feed("x" * 20_000)
    finish = asyncio.create_task(relay.finish("✅ done"))
    await asyncio.wait_for(send_started.wait(), timeout=1)

    finish.cancel()
    with pytest.raises(asyncio.CancelledError):
        await finish
    await relay.abort()

    assert not list(tmp_path.glob("*"))
    assert not {
        pending.get_name()
        for pending in asyncio.all_tasks()
        if not pending.done() and pending.get_name() == "qingssh-output-sender"
    }


@pytest.mark.asyncio
async def test_cancellation_reclaims_remote_and_removes_relay_artifacts(tmp_path: Path) -> None:
    entered          = asyncio.Event()
    remote_cancelled = asyncio.Event()

    class Manager:
        data_dir = tmp_path

        async def execute_command_stream(self, *args: Any, timeout: float) -> int:
            callback = args[4]
            assert timeout == 0
            await callback("partial output")
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                remote_cancelled.set()
                raise

    async def send_action(_action: dict[str, Any]) -> None:
        raise AssertionError("partial output must not escape after cancellation")

    session = _Session()
    task    = _start_background_job(
        session     = session,
        manager     = Manager(),
        send_action = send_action,
        command     = "long-running-command --unchanged",
        policy      = _fast_policy(),
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert remote_cancelled.is_set()
    assert not (tmp_path / "command_outputs").exists()
    assert session.get(SessionKeys.STATE) == "connected"
    assert session.get(SessionKeys.CURRENT_TASK) is None
    assert not {
        pending.get_name()
        for pending in asyncio.all_tasks()
        if not pending.done() and pending.get_name().startswith("qingssh-output-")
    }


@pytest.mark.asyncio
async def test_explicit_zero_timeout_does_not_wrap_unlimited_admin_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager                 = ssh_manager_module.SSHManager(tmp_path)
    command_seen: list[str] = []

    async def operation(*args: Any, **_kwargs: Any) -> int:
        command_seen.append(args[3])
        return 17

    async def forbidden_wait_for(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("zero timeout must not use asyncio.wait_for")

    monkeypatch.setattr(manager, "_execute_command_stream_impl", operation)
    monkeypatch.setattr(ssh_manager_module.asyncio, "wait_for", forbidden_wait_for)

    result = await manager.execute_command_stream(
        "1",
        "2",
        "srv",
        "arbitrary-admin-command --with 'quotes'",
        lambda _text: None,
        timeout=0,
    )

    assert result == 17
    assert command_seen == ["arbitrary-admin-command --with 'quotes'"]


@pytest.mark.asyncio
async def test_repeated_cancel_cannot_abort_remote_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager           = ssh_manager_module.SSHManager(tmp_path)
    operation_started = asyncio.Event()
    cleanup_started   = asyncio.Event()
    release_cleanup   = asyncio.Event()

    async def operation(*_args: Any, **_kwargs: Any) -> int:
        operation_started.set()
        await asyncio.Event().wait()
        return 0

    async def cleanup(_key: str) -> ssh_manager_module.CommandTerminationResult:
        cleanup_started.set()
        await release_cleanup.wait()
        return ssh_manager_module.CommandTerminationResult(
            found            = True,
            local_cleaned    = True,
            remote_confirmed = True,
        )

    monkeypatch.setattr(manager, "_execute_command_stream_impl", operation)
    monkeypatch.setattr(manager, "_terminate_active_command", cleanup)
    task = asyncio.create_task(
        manager.execute_command_stream(
            "1",
            "2",
            "srv",
            "long command",
            lambda _text: None,
            timeout=0,
        )
    )
    await asyncio.wait_for(operation_started.wait(), timeout=1)

    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert not {
        pending.get_name()
        for pending in asyncio.all_tasks()
        if not pending.done() and pending.get_name() == "qingssh-remote-command-cleanup"
    }
