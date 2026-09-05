"""SSH 连接、凭据和命令执行。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from tests.helpers.qingssh_test_support import (
    EXIT_CODE_TIMEOUT,
    Any,
    AsyncMock,
    MagicMock,
    Mock,
    Path,
    SessionKeys,
    _connected_session,
    _FakeChannel,
    _FakeClient,
    _SessionStub,
    asyncio,
    cast,
    json,
    pytest,
    ssh_manager_module,
    ssh_session_handlers,
)
from tests.helpers.settings_snapshot import with_settings_reader


def test_disconnect_closes_active_channel_and_jump_connection(tmp_path):
    manager = ssh_manager_module.SSHManager(tmp_path)
    key     = manager.build_connection_key("10001", "20001", "srv")
    channel = _FakeChannel()
    client  = _FakeClient()

    manager.active_channels[key] = channel
    manager.connections[key]     = client

    assert manager.disconnect("10001", "20001", "srv") is True
    assert channel.closed is True
    assert channel.sent == ["\x03"]
    assert client.closed is True
    assert client._jump_client.closed is True
    assert key not in manager.active_channels
    assert key not in manager.connections


@pytest.mark.asyncio
async def test_connect_closes_jump_host_when_target_connection_fails(monkeypatch, tmp_path):
    class _FakeProxySock:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeTransport:
        def __init__(self, sock):
            self.sock = sock

        def open_channel(self, *_args):
            return self.sock

    class _FakeJumpClient:
        instances: ClassVar[list[object]] = []

        def __init__(self):
            self.closed    = False
            self.sock      = _FakeProxySock()
            self.connected = False
            _FakeJumpClient.instances.append(self)

        def set_missing_host_key_policy(self, _policy):
            return None

        def load_system_host_keys(self):
            return None

        def get_transport(self):
            return _FakeTransport(self.sock)

        def close(self):
            self.closed = True

        def connect(self, **_kwargs):
            self.connected = True

    class _FakeTargetClient:
        def __init__(self):
            self.closed = False

        def set_missing_host_key_policy(self, _policy):
            return None

        def load_system_host_keys(self):
            return None

        def close(self):
            self.closed = True

        def connect(self, **_kwargs):
            raise ssh_manager_module.paramiko.SSHException(
                r"CR_P02_SSH_SECRET C:\private\id_ed25519 target failed"
            )

    clients = [_FakeTargetClient(), _FakeJumpClient()]

    def _client_factory():
        return clients.pop(0)

    monkeypatch.setattr(ssh_manager_module.paramiko, "SSHClient", _client_factory)
    monkeypatch.setattr(ssh_manager_module, "PARAMIKO_AVAILABLE", True)

    context            = MagicMock()
    context.request_id = "req-qingssh-connect"
    context.secrets    = {"private_exception_canary": "CR_P02_SSH_SECRET"}
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    manager._ssh_config                    = MagicMock()
    manager._ssh_config.lookup.side_effect = lambda host: {
        "hostname": host,
        "port": "22",
        "user": "root",
        "identityfile": [],
        "proxyjump": "jump-host" if host == "srv" else None,
    }
    manager.servers["srv"] = {
        "host": "srv.internal",
        "port": 22,
        "username": "root",
        "proxyjump": "jump-host",
    }

    ok, message = await manager.connect("10001", "20001", "srv")

    assert ok is False
    assert "XQ-PLUGIN-UNEXPECTED" in message
    assert "req-qingssh-connect" in message
    assert "CR_P02_SSH_SECRET" not in message
    assert "id_ed25519" not in message
    jump_client = _FakeJumpClient.instances[0]
    assert jump_client.closed is True
    assert jump_client.sock.closed is True


@pytest.mark.asyncio
async def test_manager_request_context_is_task_local(tmp_path):
    state = {}
    first = SimpleNamespace(
        state      = state,
        data_dir   = tmp_path,
        plugin_dir = tmp_path,
        logger     = Mock(),
    )
    second = SimpleNamespace(
        state      = state,
        data_dir   = tmp_path,
        plugin_dir = tmp_path,
        logger     = Mock(),
    )

    manager = await ssh_manager_module.get_manager(first)

    async def resolve(context):
        resolved = await ssh_manager_module.get_manager(context)
        await asyncio.sleep(0)
        return resolved.context

    first_context, second_context = await asyncio.gather(
        asyncio.create_task(resolve(first)),
        asyncio.create_task(resolve(second)),
    )

    assert first_context is first
    assert second_context is second
    assert manager._base_context is first


def test_authentication_failure_never_exposes_private_key_path(tmp_path):
    manager     = ssh_manager_module.SSHManager(tmp_path)
    private_key = r"C:\private\keys\production-id_ed25519"

    message = manager._authentication_failure_message(
        {
            "host": "example.com",
            "port": 22,
            "username": "root",
            "key_path": private_key,
        }
    )

    assert private_key not in message
    assert "production-id_ed25519" not in message
    assert "私钥配置" in message


@pytest.mark.asyncio
async def test_stream_failure_uses_correlated_public_error(tmp_path):
    canary             = "CR_P02_STREAM_SECRET"
    context            = MagicMock()
    context.request_id = "req-qingssh-stream"
    context.secrets    = {"private_exception_canary": canary}
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    key = manager.build_connection_key("1", "2", "srv")

    class Client:
        def get_transport(self):
            raise RuntimeError(rf"{canary} C:\private\stream.sock")

    manager.connections[key] = cast(Any, Client())
    manager.is_connected = Mock(return_value=True)
    output: list[str] = []

    async def collect(text: str) -> None:
        output.append(text)

    exit_code = await manager._execute_command_stream_impl(
        "1",
        "2",
        "srv",
        "echo ok",
        collect,
    )

    public_text = "".join(output)
    assert exit_code == ssh_manager_module.EXIT_CODE_ERROR
    assert "XQ-PLUGIN-UNEXPECTED" in public_text
    assert "req-qingssh-stream" in public_text
    assert canary not in public_text
    assert "stream.sock" not in public_text


@pytest.mark.asyncio
async def test_connect_reserves_capacity_across_concurrent_dials(monkeypatch, tmp_path):
    class _Client:
        def __init__(self):
            self.closed    = False
            self.connected = False

        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **_kwargs):
            self.connected = True

        def close(self):
            self.closed = True

    clients: list[_Client] = []

    def client_factory():
        client = _Client()
        clients.append(client)
        return client

    dial_started = asyncio.Event()
    release_dial = asyncio.Event()

    async def controlled_to_thread(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "connect":
            dial_started.set()
            await release_dial.wait()
        return func(*args, **kwargs)

    context = with_settings_reader(Mock(config={"plugins": {"qingssh": {"max_connections": 1}}}))
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    manager.servers.update(
        {
            "first": {"host": "first.internal", "port": 22, "username": "root"},
            "second": {"host": "second.internal", "port": 22, "username": "root"},
        }
    )
    monkeypatch.setattr(ssh_manager_module, "PARAMIKO_AVAILABLE", True)
    monkeypatch.setattr(ssh_manager_module.paramiko, "SSHClient", client_factory)
    monkeypatch.setattr(manager, "_load_host_keys", lambda *_args: None)
    monkeypatch.setattr(ssh_manager_module.asyncio, "to_thread", controlled_to_thread)

    first_task = asyncio.create_task(manager.connect("u1", "g1", "first"))
    await dial_started.wait()
    second_result = await manager.connect("u2", "g1", "second")
    release_dial.set()
    first_result = await first_task

    assert first_result[0] is True
    assert second_result == (False, "❌ SSH 活跃连接已达到配置上限 (1)")
    assert len(clients) == 1
    assert manager._pending_connection_keys == set()
    manager.close_all()


@pytest.mark.asyncio
async def test_connect_prunes_stale_clients_before_enforcing_capacity(monkeypatch, tmp_path):
    class _StaleClient:
        def __init__(self):
            self.closed = False

        def get_transport(self):
            return None

        def close(self):
            self.closed = True

    class _NewClient:
        def __init__(self):
            self.closed = False

        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **_kwargs):
            return None

        def close(self):
            self.closed = True

    context = Mock(config={"plugins": {"qingssh": {"max_connections": 1}}})
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    manager.servers["new"] = {
        "host": "new.internal",
        "port": 22,
        "username": "root",
    }
    stale                          = _StaleClient()
    stale_key                      = manager.build_connection_key("old-user", "g1", "old")
    manager.connections[stale_key] = cast(Any, stale)
    new_client                     = _NewClient()
    monkeypatch.setattr(ssh_manager_module, "PARAMIKO_AVAILABLE", True)
    monkeypatch.setattr(ssh_manager_module.paramiko, "SSHClient", lambda: new_client)
    monkeypatch.setattr(manager, "_load_host_keys", lambda *_args: None)

    result = await manager.connect("new-user", "g1", "new")

    assert result[0] is True
    assert stale.closed is True
    assert stale_key not in manager.connections
    manager.close_all()


@pytest.mark.asyncio
async def test_connect_closes_jump_client_when_channel_open_fails(monkeypatch, tmp_path):
    class _TargetClient:
        def __init__(self):
            self.closed = False

        def set_missing_host_key_policy(self, _policy):
            return None

        def close(self):
            self.closed = True

    class _BrokenTransport:
        def open_channel(self, *_args):
            raise OSError("jump channel failed")

    class _JumpClient(_TargetClient):
        def connect(self, **_kwargs):
            return None

        def get_transport(self):
            return _BrokenTransport()

    target_client      = _TargetClient()
    jump_client        = _JumpClient()
    clients            = [target_client, jump_client]
    context            = MagicMock()
    context.request_id = "req-qingssh-jump"
    context.secrets    = {}
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)
    manager.servers["srv"] = {
        "host": "srv.internal",
        "port": 22,
        "username": "root",
        "proxycommand": "ssh -W %h:%p jump-host",
    }
    monkeypatch.setattr(ssh_manager_module, "PARAMIKO_AVAILABLE", True)
    monkeypatch.setattr(ssh_manager_module.paramiko, "SSHClient", lambda: clients.pop(0))
    monkeypatch.setattr(manager, "_load_host_keys", lambda *_args: None)
    monkeypatch.setattr(
        manager,
        "get_ssh_config_for_host",
        lambda host: {
            "hostname": host,
            "port": 22,
            "user": "root",
            "identityfile": [],
        },
    )

    ok, message = await manager.connect("u1", "g1", "srv")

    assert ok is False
    assert "XQ-PLUGIN-UNEXPECTED" in message
    assert "req-qingssh-jump" in message
    assert "jump channel failed" not in message
    assert target_client.closed is True
    assert jump_client.closed is True
    assert manager._pending_connection_keys == set()


@pytest.mark.asyncio
async def test_cancelled_connect_closes_client_and_releases_reservation(monkeypatch, tmp_path):
    class _Client:
        def __init__(self):
            self.closed = False

        def set_missing_host_key_policy(self, _policy):
            return None

        def connect(self, **_kwargs):
            return None

        def close(self):
            self.closed = True

    client       = _Client()
    dial_started = asyncio.Event()
    release_dial = asyncio.Event()

    async def blocked_to_thread(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "connect":
            dial_started.set()
            await release_dial.wait()
        return func(*args, **kwargs)

    manager                = ssh_manager_module.SSHManager(tmp_path)
    manager.servers["srv"] = {"host": "srv.internal", "port": 22, "username": "root"}
    monkeypatch.setattr(ssh_manager_module, "PARAMIKO_AVAILABLE", True)
    monkeypatch.setattr(ssh_manager_module.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(manager, "_load_host_keys", lambda *_args: None)
    monkeypatch.setattr(ssh_manager_module.asyncio, "to_thread", blocked_to_thread)

    task = asyncio.create_task(manager.connect("u1", "g1", "srv"))
    await dial_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_dial.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.closed is True
    assert manager.connections == {}
    assert manager._pending_connection_keys == set()


@pytest.mark.asyncio
async def test_execute_command_stream_applies_timeout(monkeypatch, tmp_path):
    manager = ssh_manager_module.SSHManager(tmp_path)

    async def fake_impl(*args, **kwargs):
        await asyncio.sleep(0.05)
        return 0

    terminate = AsyncMock(
        return_value=ssh_manager_module.CommandTerminationResult(
            found            = True,
            local_cleaned    = True,
            remote_confirmed = False,
        )
    )
    output = AsyncMock()
    monkeypatch.setattr(ssh_manager_module, "COMMAND_TIMEOUT", 0.01)
    monkeypatch.setattr(manager, "_execute_command_stream_impl", fake_impl)
    monkeypatch.setattr(manager, "_terminate_active_command", terminate)

    result = await manager.execute_command_stream("10001", "20001", "srv", "sleep 1", output)

    assert result == EXIT_CODE_TIMEOUT
    terminate.assert_awaited_once_with("10001:20001:srv")
    assert "状态未知" in output.await_args.args[0]


@pytest.mark.asyncio
async def test_password_setup_is_rejected_in_group_chat():
    context = Mock(current_user_id=10001, current_group_id=20001)
    context.end_session = AsyncMock()
    session             = _SessionStub(
        {
            SessionKeys.STEP: "auth_type",
            SessionKeys.SERVER_CONFIG: {
                "name": "srv",
                "host": "example.com",
                "port": 22,
                "username": "root",
            },
        }
    )

    result = await ssh_session_handlers._handle_adding_session(
        "password",
        context,
        session,
        MagicMock(),
    )

    assert "私聊" in result[0]["data"]["text"]
    assert session.get(SessionKeys.STEP) == "auth_type"
    context.end_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_password_setup_keeps_plaintext_out_of_session_state():
    context = Mock(current_user_id=10001, current_group_id=None)
    context.end_session = AsyncMock()
    manager             = MagicMock()
    manager.add_server = AsyncMock(return_value=True)
    session = _SessionStub(
        {
            SessionKeys.STEP: "password",
            SessionKeys.SERVER_CONFIG: {
                "name": "srv",
                "host": "example.com",
                "port": 22,
                "username": "root",
                "auth_type": "password",
            },
        }
    )

    await ssh_session_handlers._handle_adding_session(" top-secret ", context, session, manager)

    kwargs = manager.add_server.await_args.kwargs
    assert kwargs["password"] == " top-secret "
    assert "password" not in session.get(SessionKeys.SERVER_CONFIG)
    context.end_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_corrupt_add_session_is_ended_instead_of_raising():
    context = Mock(current_user_id=10001, current_group_id=None)
    context.end_session = AsyncMock()
    session             = _SessionStub(
        {
            SessionKeys.STEP: "password",
            SessionKeys.SERVER_CONFIG: {},
        }
    )

    result = await ssh_session_handlers._handle_adding_session(
        "secret",
        context,
        session,
        MagicMock(),
    )

    assert "状态无效" in result[0]["data"]["text"]
    context.end_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_add_failure_does_not_report_success_or_end_session():
    context = Mock(current_user_id=10001, current_group_id=None)
    context.end_session = AsyncMock()
    manager             = MagicMock()
    manager.add_server = AsyncMock(return_value=False)
    session = _SessionStub(
        {
            SessionKeys.STEP: "auth_type",
            SessionKeys.SERVER_CONFIG: {
                "name": "srv",
                "host": "example.com",
                "port": 22,
                "username": "root",
            },
        }
    )

    result = await ssh_session_handlers._handle_adding_session("agent", context, session, manager)

    assert "保存失败" in result[0]["data"]["text"]
    context.end_session.assert_not_awaited()


def test_session_without_plugin_identity_cannot_own_qingssh_job():
    class AnonymousSession:
        def get(self, key, default=None):
            values = {
                SessionKeys.SERVER_NAME: "srv",
                SessionKeys.STATE: "executing",
                SessionKeys.CURRENT_TASK: "job",
            }
            return values.get(key, default)

    assert not ssh_session_handlers._session_owns_job(
        cast(Any, AnonymousSession()),
        server_name = "srv",
        job_id      = "job",
    )


@pytest.mark.asyncio
async def test_connected_session_rejects_overlong_command_before_starting_job():
    context = Mock(current_user_id=10001, current_group_id=20001, config={})
    context.end_session = AsyncMock()
    session             = _SessionStub(
        {
            SessionKeys.SERVER_NAME: "srv",
            SessionKeys.STATE: "connected",
            SessionKeys.COMMAND_COUNT: 0,
        }
    )
    manager                           = MagicMock()
    manager.is_connected.return_value = True

    result = await ssh_session_handlers._handle_connected_session(
        "x" * 10_001,
        context,
        session,
        manager,
    )

    assert "命令过长" in result[0]["data"]["text"]
    assert session.get(SessionKeys.STATE) == "connected"
    assert not ssh_session_handlers._COMMAND_JOBS


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["help", "/help", "ssh帮助", "插件帮助", "帮助"])
async def test_connected_session_help_aliases_do_not_start_remote_commands(alias: str):
    context = Mock(current_user_id=10001, current_group_id=20001)
    context.end_session               = AsyncMock()
    session                           = _connected_session()
    manager                           = MagicMock()
    manager.is_connected.return_value = True

    result = await ssh_session_handlers._handle_connected_session(
        alias,
        context,
        session,
        manager,
    )

    assert "SSH 会话帮助" in result[0]["data"]["text"]
    assert "showimg <路径或通配符> [--page N]" in result[0]["data"]["text"]
    assert "*、?、[]" in result[0]["data"]["text"]
    assert "每页 5 张" in result[0]["data"]["text"]
    assert session.get(SessionKeys.STATE) == "connected"
    assert not ssh_session_handlers._COMMAND_JOBS


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["showimg", "SHOWIMG", "showimg\t"])
async def test_showimg_without_pattern_returns_usage_instead_of_running_remote_command(
    command: str,
):
    context = Mock(current_user_id=10001, current_group_id=20001)
    context.end_session               = AsyncMock()
    session                           = _connected_session()
    manager                           = MagicMock()
    manager.is_connected.return_value = True
    manager.list_files                = AsyncMock()

    result = await ssh_session_handlers._handle_connected_session(
        command,
        context,
        session,
        manager,
    )

    assert "用法: showimg <路径或通配符> [--page N]" in result[0]["data"]["text"]
    manager.list_files.assert_not_awaited()
    assert session.get(SessionKeys.STATE) == "connected"
    assert not ssh_session_handlers._COMMAND_JOBS


@pytest.mark.parametrize(
    ("command", "expected_pattern", "expected_page"),
    [
        ("showimg ./*", "./*", 1),
        ("showimg ./* --page 2", "./*", 2),
        ("showimg ./plots with spaces/*.png --page=3", "./plots with spaces/*.png", 3),
    ],
)
def test_showimg_request_parser_supports_paths_and_explicit_pages(
    command: str,
    expected_pattern: str,
    expected_page: int,
):
    request = ssh_session_handlers._parse_showimg_request(command)

    assert request.pattern == expected_pattern
    assert request.page == expected_page


@pytest.mark.parametrize(
    ("pattern", "cwd", "expected"),
    [
        ("./*", "/remote", ("/remote", "*")),
        ("./plots/*.png", "/remote", ("/remote/plots", "*.png")),
        ("../charts/plot-?.jpg", "/remote/work", ("/remote/charts", "plot-?.jpg")),
        ("/srv/charts/*.webp", "/remote", ("/srv/charts", "*.webp")),
    ],
)
def test_showimg_listing_resolves_explicit_relative_and_absolute_directories(
    pattern: str,
    cwd: str | None,
    expected: tuple[str, str],
):
    assert ssh_session_handlers._resolve_showimg_listing(pattern, cwd) == expected


@pytest.mark.parametrize(
    "command",
    [
        "showimg ./* --page 0",
        "showimg ./* --page nope",
        "showimg ./* --page",
        "showimg --page 2 ./*",
    ],
)
def test_showimg_request_parser_rejects_invalid_page_options(command: str):
    with pytest.raises(ssh_session_handlers._ShowImageInputError):
        ssh_session_handlers._parse_showimg_request(command)


def test_showimg_listing_rejects_wildcards_in_directory_component():
    with pytest.raises(
        ssh_session_handlers._ShowImageInputError,
        match="目录部分需要明确路径",
    ):
        ssh_session_handlers._resolve_showimg_listing("./run-*/plot.png", "/remote")


@pytest.mark.asyncio
async def test_list_files_applies_wildcard_and_returns_stable_filename_order(tmp_path: Path):
    class SFTP:
        def __init__(self):
            self.closed = False

        @staticmethod
        def listdir(_remote_dir):
            return ["plot-2.png", "notes.txt", "PLOT-0.png", "plot-10.png", "plot-1.png"]

        def close(self):
            self.closed = True

    class Client:
        def __init__(self, sftp):
            self.sftp = sftp

        def open_sftp(self):
            return self.sftp

    manager                  = ssh_manager_module.SSHManager(tmp_path)
    sftp                     = SFTP()
    key                      = manager.build_connection_key("10001", "20001", "srv")
    manager.connections[key] = cast(Any, Client(sftp))
    manager.is_connected = Mock(return_value=True)

    success, files = await manager.list_files(
        "10001",
        "20001",
        "srv",
        "/remote",
        "plot-*.png",
    )

    assert success is True
    assert files == ["plot-1.png", "plot-10.png", "plot-2.png"]
    assert sftp.closed is True


@pytest.mark.asyncio
async def test_list_files_keeps_matches_beyond_historical_first_hundred(tmp_path: Path):
    filenames = [f"plot-{index:03}.png" for index in range(125, 0, -1)]

    class SFTP:
        @staticmethod
        def listdir(_remote_dir):
            return filenames

        @staticmethod
        def close():
            return None

    class Client:
        @staticmethod
        def open_sftp():
            return SFTP()

    manager                  = ssh_manager_module.SSHManager(tmp_path)
    key                      = manager.build_connection_key("10001", "20001", "srv")
    manager.connections[key] = cast(Any, Client())
    manager.is_connected = Mock(return_value=True)

    success, files = await manager.list_files(
        "10001",
        "20001",
        "srv",
        "/remote",
        "plot-*.png",
    )

    assert success is True
    assert len(files) == 125
    assert files[0] == "plot-001.png"
    assert files[-1] == "plot-125.png"


@pytest.mark.asyncio
async def test_showimg_path_wildcard_sends_requested_page_with_global_positions(tmp_path: Path):
    context = Mock(
        current_user_id  = 10001,
        current_group_id = 20001,
        plugin_dir       = tmp_path,
    )
    context.end_session = AsyncMock()
    context.send_action = AsyncMock()
    session             = _SessionStub(
        {
            SessionKeys.SERVER_NAME: "srv",
            SessionKeys.CWD: "/remote",
        }
    )
    manager                           = MagicMock()
    manager.is_connected.return_value = True
    manager.list_files                = AsyncMock(
        return_value=(
            True,
            [
                "notes.txt",
                *[f"plot-{index:02}.png" for index in range(12, 0, -1)],
            ],
        )
    )

    downloaded_remote_paths: list[str] = []

    async def download(_user, _group, _server, remote, local, **_kwargs):
        downloaded_remote_paths.append(remote)
        Path(local).write_bytes(b"image")
        return True, "ok"

    manager.download_file = AsyncMock(side_effect=download)

    result = await ssh_session_handlers._handle_showimg_command(
        "showimg ./plots/plot-* --page 2",
        context,
        session,
        manager,
    )

    expected_filenames = [
        "plot-06.png",
        "plot-07.png",
        "plot-08.png",
        "plot-09.png",
        "plot-10.png",
    ]
    manager.list_files.assert_awaited_once_with(
        "10001",
        "20001",
        "srv",
        "/remote/plots",
        "plot-*",
    )
    assert downloaded_remote_paths == [f"/remote/plots/{name}" for name in expected_filenames]
    assert context.send_action.await_count == len(expected_filenames)

    for global_index, (expected_filename, action_call) in enumerate(
        zip(expected_filenames, context.send_action.await_args_list, strict=True),
        6,
    ):
        action  = action_call.args[0]
        message = action["params"]["message"]
        assert [segment["type"] for segment in message] == ["text", "image"]
        assert message[0]["data"]["text"] == f"📷 {global_index}/12\n{expected_filename}\n"

    response_text = result[0]["data"]["text"]
    assert "第 2/3 页已按文件名顺序发送 5 张图片" in response_text
    assert "上一页：showimg ./plots/plot-* --page 1" in response_text
    assert "下一页：showimg ./plots/plot-* --page 3" in response_text
    assert list((tmp_path / "data" / "images").iterdir()) == []


@pytest.mark.asyncio
async def test_showimg_rejects_page_beyond_matching_images_before_download(tmp_path: Path):
    context = Mock(
        current_user_id  = 10001,
        current_group_id = 20001,
        plugin_dir       = tmp_path,
    )
    context.end_session = AsyncMock()
    context.send_action = AsyncMock()
    session             = _SessionStub(
        {
            SessionKeys.SERVER_NAME: "srv",
            SessionKeys.CWD: "/remote",
        }
    )
    manager                           = MagicMock()
    manager.is_connected.return_value = True
    manager.list_files                = AsyncMock(
        return_value=(True, [f"plot-{index}.png" for index in range(1, 7)])
    )
    manager.download_file = AsyncMock()

    result = await ssh_session_handlers._handle_showimg_command(
        "showimg ./*.png --page 3",
        context,
        session,
        manager,
    )

    assert "页码超出范围：共 2 页、6 张图片" in result[0]["data"]["text"]
    manager.download_file.assert_not_awaited()
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_showimg_send_failure_still_removes_downloaded_temp_file(tmp_path: Path):
    context = Mock(
        current_user_id  = 10001,
        current_group_id = 20001,
        plugin_dir       = tmp_path,
    )
    context.end_session = AsyncMock()
    context.send_action = AsyncMock(side_effect=RuntimeError("send failed"))
    session = _SessionStub(
        {
            SessionKeys.SERVER_NAME: "srv",
            SessionKeys.CWD: "/remote",
        }
    )
    manager                           = MagicMock()
    manager.is_connected.return_value = True
    manager.list_files = AsyncMock(return_value=(True, ["plot.png"]))

    async def download(_user, _group, _server, _remote, local, **_kwargs):
        Path(local).write_bytes(b"png")
        return True, "ok"

    manager.download_file = AsyncMock(side_effect=download)

    with pytest.raises(RuntimeError, match="send failed"):
        await ssh_session_handlers._handle_showimg_command(
            "showimg plot.png",
            context,
            session,
            manager,
        )

    assert list((tmp_path / "data" / "images").iterdir()) == []


@pytest.mark.asyncio
async def test_server_config_never_persists_plaintext_password(tmp_path):
    stored             = {}
    context            = Mock()
    context.set_secret = lambda key, value: stored.__setitem__(key, value)
    manager = ssh_manager_module.SSHManager(tmp_path, context=context)

    await manager.add_server("srv", "example.com", password="top-secret")

    payload = json.loads((tmp_path / "servers.json").read_text(encoding="utf-8"))
    assert "password" not in payload["srv"]
    assert payload["srv"]["password_ref"] in stored
