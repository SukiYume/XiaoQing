"""小青 agent 能力：和对应命令同一套权限，每次调用重新确认，并按固定服务契约转发。"""

from __future__ import annotations

import asyncio
import json

import tests.helpers.app_test_support as _fixture_support
from core.models import PluginManifest
from core.router import CommandSpec, build_command_catalog_node
from core.session import Session
from tests.helpers.app_test_support import (
    ModuleType,
    Path,
    PluginDefinition,
    PluginServiceDefinition,
    XiaoQingApp,
    _plugin_context_for,
    _register_test_loaded_plugin,
    pytest,
)

temp_app_root = _fixture_support.temp_app_root

ADMIN = 12345
OTHER = 55555


def _principal(
    app: XiaoQingApp,
    user_id: int,
    group_id: int | None = None,
    role: str            = "",
):
    sender = {"user_id": user_id, **({"role": role} if role else {})}
    return app.issue_user_principal(
        {"sender": sender},
        user_id    = user_id,
        group_id   = group_id,
        is_private = group_id is None,
    )


def _install_service(
    app: XiaoQingApp,
    owner: str,
    service: str,
    caller: str,
    callback,
    concurrency: str = "parallel",
):
    module = ModuleType(f"plugins.{owner}.main")
    setattr(module, callback.__name__, callback)
    definition = PluginDefinition(
        name        = owner,
        version     = "1.0.0",
        entry       = "main.py",
        commands    = [],
        schedule    = [],
        concurrency = concurrency,
        services    = (
            PluginServiceDefinition(
                name     = service,
                callback = callback.__name__,
                callers  = frozenset({caller}),
            ),
        ),
    )
    _register_test_loaded_plugin(app, definition, module)


def _publish_tool_commands(app: XiaoQingApp, **permissions: str) -> None:
    """发布 agent 工具对应的命令；默认与生产 manifest 一致，都只给 Bot 管理员。"""

    async def unused(_command, _args, _event, _context):
        raise AssertionError("tool command handlers are not called here")

    for plugin, trigger in (("shell", "shell"), ("qingssh", "ssh"), ("codex", "codex")):
        app.router.clear_plugin(plugin)
        _register_command(
            app, plugin, trigger, unused, permission=permissions.get(plugin, "bot_admin")
        )


@pytest.mark.unit
def test_agent_capabilities_follow_the_matching_command_permissions(temp_app_root: Path) -> None:
    """对话和命令执行给每个用户（命令自己再查权限）；Codex、SSH 按 /codex、/ssh 的声明授权。"""

    app = XiaoQingApp(temp_app_root)
    _publish_tool_commands(app)
    for user_id, admin in ((ADMIN, True), (OTHER, False)):
        chat = _plugin_context_for(
            app,
            "xiaoqing_chat",
            user_id       = user_id,
            principal     = _principal(app, user_id),
            uses_services = frozenset({"agent.handle_turn"}),
        )
        agent = _plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id               = user_id,
            principal             = _principal(app, user_id),
            manifest_capabilities = frozenset({"command_runner"}),
            uses_services         = frozenset({"codex.agent_task", "qingssh.agent_command"}),
        )
        assert chat.capabilities.agent_turn is not None
        assert agent.capabilities.command_runner is not None
        assert (agent.capabilities.codex_agent is not None) is admin
        assert (agent.capabilities.ssh_agent is not None) is admin

    undeclared = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id   = ADMIN,
        principal = _principal(app, ADMIN),
    )
    assert undeclared.capabilities.agent_turn is None
    assert undeclared.capabilities.command_runner is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_turn_reaches_agent_with_the_speakers_principal(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    principal = _principal(app, ADMIN, group_id=777)
    seen: list = []

    async def handle_turn(turn, context):
        seen.append((turn, context))
        return {"handled": True, "segments": [{"type": "text", "data": {"text": "ok"}}]}

    _install_service(app, "xiaoqing_agent", "agent.handle_turn", "xiaoqing_chat", handle_turn)
    chat = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = ADMIN,
        group_id      = 777,
        principal     = principal,
        uses_services = frozenset({"agent.handle_turn"}),
    )
    capability = chat.capabilities.agent_turn
    result     = await capability.handle_turn({"text": "画个图"})
    assert result == {"handled": True, "segments": [{"type": "text", "data": {"text": "ok"}}]}
    turn, target = seen[-1]
    assert turn == {"text": "画个图"}
    assert target.plugin_name == "xiaoqing_agent"
    assert target.principal is principal
    assert target.current_group_id == 777

    with pytest.raises(TypeError):
        await capability.handle_turn(["not", "a", "mapping"])

    # 不再是 Bot 管理员也照样交给 agent：能做什么由 agent 按发起人的权限决定。
    app.identity_service.load_admins({})
    await capability.handle_turn({"text": "再画一个"})
    assert len(seen) == 2 and seen[-1][1].capabilities.is_bot_admin is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_turn_none_means_ordinary_chat_and_bad_results_are_rejected(
    temp_app_root: Path,
) -> None:
    app     = XiaoQingApp(temp_app_root)
    results = [None, "not a mapping"]

    async def handle_turn(_turn, _context):
        return results.pop(0)

    _install_service(app, "xiaoqing_agent", "agent.handle_turn", "xiaoqing_chat", handle_turn)
    capability = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = ADMIN,
        principal     = _principal(app, ADMIN),
        uses_services = frozenset({"agent.handle_turn"}),
    ).capabilities.agent_turn
    assert await capability.handle_turn({"text": "在吗"}) is None
    with pytest.raises(TypeError):
        await capability.handle_turn({"text": "在吗"})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_turn_reports_missing_agent_plugin_as_unavailable(temp_app_root: Path) -> None:
    app        = XiaoQingApp(temp_app_root)
    capability = _plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = ADMIN,
        principal     = _principal(app, ADMIN),
        uses_services = frozenset({"agent.handle_turn"}),
    ).capabilities.agent_turn
    with pytest.raises(RuntimeError, match="unavailable"):
        await capability.handle_turn({"text": "画个图"})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_codex_and_ssh_agent_services_validate_and_forward_requests(
    temp_app_root: Path,
) -> None:
    app   = XiaoQingApp(temp_app_root)
    codex = []
    ssh   = []

    async def agent_task_service(request, context):
        codex.append((request, context.plugin_name))
        return {"status": "done", "final_text": "ok", "images": []}

    async def agent_command_service(request, context):
        ssh.append((request, context.plugin_name))
        if request["op"] == "list":
            return [{"name": "lab"}]
        return {"ok": True, "exit_code": 0, "output": "hi"}

    _install_service(app, "codex", "codex.agent_task", "xiaoqing_agent", agent_task_service)
    _install_service(
        app, "qingssh", "qingssh.agent_command", "xiaoqing_agent", agent_command_service
    )
    # 装服务会按插件重建命令表，工具对应的命令在之后发布。
    _publish_tool_commands(app)
    agent = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id       = ADMIN,
        principal     = _principal(app, ADMIN),
        uses_services = frozenset({"codex.agent_task", "qingssh.agent_command"}),
    )

    result = await agent.capabilities.codex_agent.run_task(
        session         = "agent-u12345",
        prompt          = " 写个脚本 ",
        cwd             = "  ",
        timeout_seconds = 60,
    )
    assert result["final_text"] == "ok"
    assert codex[-1] == (
        {"session": "agent-u12345", "prompt": "写个脚本", "cwd": None, "timeout_seconds": 60.0},
        "codex",
    )
    for bad in ({"prompt": ""}, {"timeout_seconds": 0}, {"timeout_seconds": True}):
        kwargs = {"session": "s", "prompt": "p", "cwd": None, "timeout_seconds": 5} | bad
        with pytest.raises(ValueError):
            await agent.capabilities.codex_agent.run_task(**kwargs)

    assert await agent.capabilities.ssh_agent.list_servers() == [{"name": "lab"}]
    assert (
        await agent.capabilities.ssh_agent.run(server="lab", command="uptime", timeout_seconds=30)
    )["output"] == "hi"
    assert ssh[-1] == (
        {"op": "run", "server": "lab", "command": "uptime", "timeout_seconds": 30.0},
        "qingssh",
    )
    with pytest.raises(ValueError):
        await agent.capabilities.ssh_agent.run(server="lab", command=" ", timeout_seconds=30)

    # 签发后撤掉管理员：和直接用 /codex、/ssh 一样，下一次调用就不再放行。
    app.identity_service.load_admins({})
    with pytest.raises(PermissionError):
        await agent.capabilities.ssh_agent.list_servers()
    with pytest.raises(PermissionError):
        await agent.capabilities.codex_agent.run_task(
            session="s", prompt="p", cwd=None, timeout_seconds=5
        )
    assert len(codex) == 1


def _register_command(
    app: XiaoQingApp,
    plugin: str,
    trigger: str,
    handler,
    contexts: tuple[str, ...] = ("private", "group"),
    permission: str           = "public",
) -> None:
    (app.plugins_dir / plugin).mkdir(parents=True, exist_ok=True)
    root = build_command_catalog_node(
        plugin,
        {
            "name": trigger,
            "triggers": [trigger],
            "help": "测试命令",
            "usage": f"/{trigger} [参数]",
            "contexts": list(contexts),
            "permission": permission,
            "examples": [f"/{trigger}"],
            "invalid_examples": [f"/{trigger} bad"],
        },
        root=True,
    )
    app.router.register(
        CommandSpec(
            plugin     = plugin,
            name       = trigger,
            triggers   = [trigger],
            help_text  = "测试命令",
            admin_only = permission == "bot_admin",
            handler    = handler,
            usage      = f"/{trigger} [参数]",
            catalog    = root,
        )
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_executes_published_commands_as_the_speaker(
    temp_app_root: Path,
) -> None:
    app   = XiaoQingApp(temp_app_root)
    calls = []

    async def echo(command, args, event, context):
        calls.append((command, args, event, context))
        return [{"type": "text", "data": {"text": f"echo:{args}"}}]

    async def agent_cmd(_command, _args, _event, _context):
        raise AssertionError("agent must not call its own commands")

    _register_command(app, "echo", "echo", echo)
    _register_command(app, "xiaoqing_agent", "agent", agent_cmd)
    runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id  = ADMIN,
        group_id = 888,
        principal=_principal(app, ADMIN, group_id=888),
        manifest_capabilities=frozenset({"command_runner"}),
    ).capabilities.command_runner

    assert await runner.run("/echo 你好") == [{"type": "text", "data": {"text": "echo:你好"}}]
    _command, args, event, context = calls[-1]
    assert args == "你好"
    assert event["user_id"] == ADMIN and event["group_id"] == 888
    assert event["message_type"] == "group"
    assert "nickname" not in event["sender"]
    assert context.plugin_name == "echo"
    assert context.capabilities.is_bot_admin is True

    # 发起人的显示名只用于显示，规整成一行后放进 sender，和真实消息的字段一致。
    await runner.run("/echo 名字", sender_name=" 阿青\n小号 ")
    assert calls[-1][2]["sender"]["nickname"] == "阿青 小号"

    assert (await runner.run("echo 不带前缀也行"))[0]["data"]["text"] == "echo:不带前缀也行"
    assert "未知命令" in (await runner.run("/nope"))[0]["data"]["text"]
    assert "不能从这里调用" in (await runner.run("/agent status"))[0]["data"]["text"]
    with pytest.raises(ValueError):
        await runner.run("   ")

    # 撤掉管理员后命令照样执行，但身份跟着变：插件看到的不再是 Bot 管理员。
    app.identity_service.load_admins({})
    assert (await runner.run("/echo again"))[0]["data"]["text"] == "echo:again"
    assert calls[-1][3].capabilities.is_bot_admin is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_applies_each_commands_own_permission(temp_app_root: Path) -> None:
    """agent 替谁执行命令，就按谁的权限：和本人直接发送同一处检查。"""

    app            = XiaoQingApp(temp_app_root)
    ran: list[str] = []

    async def record(command, args, event, context):
        ran.append(command)
        return [{"type": "text", "data": {"text": f"ran:{command}"}}]

    _register_command(app, "shell", "shell", record, permission="bot_admin")
    _register_command(app, "qingpet", "petadmin", record, permission="group_admin")
    _register_command(app, "echo", "echo", record)

    def runner(user_id: int, role: str = ""):
        return _plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id  = user_id,
            group_id = 888,
            principal=_principal(app, user_id, group_id=888, role=role),
            manifest_capabilities=frozenset({"command_runner"}),
        ).capabilities.command_runner

    member = runner(OTHER, role="member")
    assert (await member.run("/echo hi"))[0]["data"]["text"] == "ran:echo"
    assert (await member.run("/shell dir"))[0]["data"]["text"] == "权限不足"
    assert "群管理员" in (await member.run("/petadmin"))[0]["data"]["text"]

    group_admin = runner(OTHER, role="admin")
    assert (await group_admin.run("/petadmin"))[0]["data"]["text"] == "ran:petadmin"
    assert (await group_admin.run("/shell dir"))[0]["data"]["text"] == "权限不足"

    bot_admin = runner(ADMIN)
    assert (await bot_admin.run("/shell dir"))[0]["data"]["text"] == "ran:shell"
    assert ran == ["echo", "petadmin", "shell"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_events_look_like_real_messages(temp_app_root: Path) -> None:
    from core.interfaces import COMMAND_RUNNER_EVENT_KEY

    app    = XiaoQingApp(temp_app_root)
    events = []

    async def gift(_command, args, event, _context):
        events.append((args, event))
        return [{"type": "text", "data": {"text": "ok"}}]

    _register_command(app, "qingpet", "gift", gift)
    runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = _principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner

    await runner.run("/gift [CQ:at,qq=10086] 小鱼干")
    args, event = events[-1]
    assert args == "小鱼干"
    assert event["raw_message"] == "/gift 小鱼干"
    assert event["message"] == [
        {"type": "text", "data": {"text": "/gift 小鱼干"}},
        {"type": "at", "data": {"qq": "10086"}},
    ]
    assert event[COMMAND_RUNNER_EVENT_KEY] == "xiaoqing_agent"

    await runner.run("gift 不带前缀")
    assert events[-1][1]["raw_message"] == "/gift 不带前缀"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_explains_context_restricted_commands(temp_app_root: Path) -> None:
    app   = XiaoQingApp(temp_app_root)
    calls = []

    async def ledger(_command, args, _event, _context):
        calls.append(args)
        return [{"type": "text", "data": {"text": "账本"}}]

    _register_command(app, "pendo", "ledger", ledger, contexts=("private",))
    group_runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id  = ADMIN,
        group_id = 888,
        principal=_principal(app, ADMIN, group_id=888),
        manifest_capabilities=frozenset({"command_runner"}),
    ).capabilities.command_runner
    reply = await group_runner.run("/ledger list 2026-09")
    assert reply == [
        {"type": "text", "data": {"text": "/ledger 只能在私聊里使用，当前是群聊，这里不能执行。"}}
    ]
    assert calls == []

    private_runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = _principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner
    assert (await private_runner.run("/ledger list 2026-09"))[0]["data"]["text"] == "账本"
    assert calls == ["list 2026-09"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_closes_sessions_opened_by_the_command(temp_app_root: Path) -> None:
    app = XiaoQingApp(temp_app_root)

    async def game(_command, _args, _event, context):
        await context.create_session({"step": 1})
        return [{"type": "text", "data": {"text": "游戏开始，请输入数字"}}]

    _register_command(app, "guess_number", "guess", game)
    runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = _principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner

    reply = await runner.run("/guess")
    assert [segment["data"]["text"] for segment in reply] == [
        "游戏开始，请输入数字",
        "（这条命令停在了等待回复的交互步骤，操作没有完成，会话已结束。请改用一次性写法，比如补全参数或使用跳过确认的选项；需要发起人决定的，先把情况告诉发起人。）",
    ]
    assert await app.session_manager.peek(ADMIN, None) is None

    existing = await app.session_manager.create(ADMIN, None, "qingssh", initial_data={"k": 1})
    assert isinstance(existing, Session)
    seen_by_plugin: list = []

    async def peek_then_game(_command, _args, _event, context):
        seen_by_plugin.append(await context.get_session())
        await context.create_session({"step": 1})
        return [{"type": "text", "data": {"text": "游戏开始，请输入数字"}}]

    _register_command(app, "guess_number", "guess2", peek_then_game)
    await runner.run("/guess2")

    kept = await app.session_manager.peek(ADMIN, None)
    assert kept is not None
    assert (kept.session_id, kept.plugin_name, kept.data) == (
        existing.session_id,
        "qingssh",
        {"k": 1},
    )
    # 命令看不到用户正在进行的交互，也不会把它当成自己的会话继续。
    assert seen_by_plugin == [None]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_lets_the_plugin_release_a_discarded_sessions_resources(
    temp_app_root: Path,
) -> None:
    """命令开了交互会话就被收回时，和用户发“退出”一样交给插件的 close_session 收尾。"""

    app       = XiaoQingApp(temp_app_root)
    resources = []
    closed    = []

    async def connect(_command, _args, _event, context):
        resources.append("connection")
        await context.create_session({"server": "lab"})
        return [{"type": "text", "data": {"text": "已连接 lab"}}]

    async def close_session(_event, context, session):
        closed.append((session.plugin_name, session.get("server"), context.current_user_id))
        resources.remove("connection")

    module               = ModuleType("plugins.resource_plugin.main")
    module.close_session = close_session  # type: ignore[attr-defined]
    _register_test_loaded_plugin(
        app,
        PluginDefinition(
            name        = "resource_plugin",
            version     = "1.0.0",
            entry       = "main.py",
            commands    = [],
            schedule    = [],
            concurrency = "sequential",
        ),
        module,
    )
    _register_command(app, "resource_plugin", "connect", connect)
    own = await app.session_manager.create(ADMIN, None, "qingssh", initial_data={"k": 1})
    runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = _principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner

    reply = await runner.run("/connect lab")

    assert "会话已结束" in reply[-1]["data"]["text"]
    assert closed == [("resource_plugin", "lab", ADMIN)]
    assert resources == []
    kept = await app.session_manager.peek(ADMIN, None)
    assert kept is not None and kept.session_id == own.session_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_long_ssh_command_waits_outside_the_sequential_plugin_gate(
    temp_app_root: Path,
) -> None:
    app      = XiaoQingApp(temp_app_root)
    loop     = asyncio.get_running_loop()
    remote   = loop.create_future()
    requests = []

    async def agent_command_service(request, context):
        requests.append(request["op"])
        if request["op"] == "list":
            return [{"name": "lab"}]
        return remote

    _install_service(
        app,
        "qingssh",
        "qingssh.agent_command",
        "xiaoqing_agent",
        agent_command_service,
        concurrency="sequential",
    )
    _publish_tool_commands(app)
    ssh = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id       = ADMIN,
        principal     = _principal(app, ADMIN),
        uses_services = frozenset({"qingssh.agent_command"}),
    ).capabilities.ssh_agent

    running = asyncio.create_task(ssh.run(server="lab", command="sleep 600", timeout_seconds=600))
    while requests != ["run"]:
        await asyncio.sleep(0)
    # 远端命令还没结束，同一个串行插件的其他请求照常完成。
    assert await asyncio.wait_for(ssh.list_servers(), timeout=2) == [{"name": "lab"}]
    remote.set_result({"ok": True, "exit_code": 0, "output": "done"})
    assert (await running)["output"] == "done"

    # 调用方取消时，交回的工作一并取消；取消落在通道内（服务刚启动）也一样。
    for settle_rounds in (0, 20):
        remote  = loop.create_future()
        before  = requests.count("run")
        waiting = asyncio.create_task(
            ssh.run(server="lab", command="sleep 600", timeout_seconds=600)
        )
        while requests.count("run") == before:
            await asyncio.sleep(0)
        for _ in range(settle_rounds):
            await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        for _ in range(5):
            await asyncio.sleep(0)
        assert remote.cancelled()


@pytest.mark.unit
def test_agent_manifest_contracts_are_closed_over_the_agent_plugins() -> None:
    def manifest(name: str, **fields) -> dict:
        return {"name": name, "entry": "main.py", **fields}

    PluginManifest.model_validate(
        manifest(
            "xiaoqing_agent",
            capabilities  = ["command_runner"],
            uses_services = ["codex.agent_task", "qingssh.agent_command"],
            services      = [
                {
                    "name": "agent.handle_turn",
                    "callback": "handle_turn",
                    "callers": ["xiaoqing_chat"],
                }
            ],
        )
    )
    PluginManifest.model_validate(manifest("xiaoqing_chat", uses_services=["agent.handle_turn"]))
    for invalid in (
        manifest("shell", capabilities=["command_runner"]),
        manifest("shell", uses_services=["agent.handle_turn"]),
        manifest("pendo", uses_services=["codex.agent_task"]),
        manifest(
            "shell",
            services=[
                {
                    "name": "agent.handle_turn",
                    "callback": "handle_turn",
                    "callers": ["xiaoqing_chat"],
                }
            ],
        ),
        manifest(
            "xiaoqing_agent",
            services=[
                {"name": "agent.handle_turn", "callback": "handle_turn", "callers": ["smalltalk"]}
            ],
        ),
    ):
        with pytest.raises(ValueError):
            PluginManifest.model_validate(invalid)


@pytest.mark.unit
def test_shipped_manifests_declare_the_agent_bridges() -> None:
    root = Path(__file__).resolve().parents[2] / "plugins"

    def load(name: str) -> PluginManifest:
        return PluginManifest.model_validate(
            json.loads((root / name / "plugin.json").read_text(encoding="utf-8"))
        )

    assert set(load("xiaoqing_chat").uses_services) == {
        "agent.handle_turn",
        "agent.report_delivery",
    }
    agent = load("xiaoqing_agent")
    assert set(agent.uses_services) == {"codex.agent_task", "qingssh.agent_command"}
    assert [service.name for service in agent.services] == [
        "agent.handle_turn",
        "agent.report_delivery",
    ]
    assert agent.capabilities == ["command_runner", "local_tools"]
    assert [service.name for service in load("codex").services] == [
        "codex.enqueue_arxiv_summary",
        "codex.agent_task",
    ]
    assert [service.name for service in load("qingssh").services] == ["qingssh.agent_command"]
    root_command = agent.commands[0]
    assert root_command.admin_only is False and root_command.permission == "public"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_local_tools_follow_the_shell_permission_and_are_rechecked(
    temp_app_root: Path,
) -> None:
    """本机代码执行与文件读写跟随 /shell 的声明：现在只给 Bot 管理员，撤权后下一次调用即拒绝。"""

    app = XiaoQingApp(temp_app_root)
    _publish_tool_commands(app)

    def agent(user_id: int, capabilities: frozenset[str]):
        return _plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id               = user_id,
            principal             = _principal(app, user_id),
            manifest_capabilities = capabilities,
        ).capabilities

    declared = frozenset({"command_runner", "local_tools"})
    assert agent(OTHER, declared).local_tools is None
    assert agent(ADMIN, frozenset({"command_runner"})).local_tools is None
    tools = agent(ADMIN, declared).local_tools
    assert tools is not None
    tools.ensure_authorized()

    app.identity_service.load_admins({})
    with pytest.raises(PermissionError):
        tools.ensure_authorized()


@pytest.mark.unit
def test_agent_tools_change_with_the_command_declarations(temp_app_root: Path) -> None:
    """只有一处规则：改了 /shell、/ssh、/codex 的声明，agent 的工具跟着变。"""

    app = XiaoQingApp(temp_app_root)

    def agent():
        return _plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id               = OTHER,
            principal             = _principal(app, OTHER),
            manifest_capabilities = frozenset({"command_runner", "local_tools"}),
            uses_services         = frozenset({"codex.agent_task", "qingssh.agent_command"}),
        ).capabilities

    # 命令没有发布（插件没加载）时谁都拿不到对应工具。
    assert agent().local_tools is None and agent().ssh_agent is None

    _publish_tool_commands(app, shell="public")
    tools = agent()
    assert tools.local_tools is not None
    assert tools.ssh_agent is None and tools.codex_agent is None

    # 声明改回只给 Bot 管理员后，已经发出的能力下一次调用就被拒绝。
    _publish_tool_commands(app)
    with pytest.raises(PermissionError):
        tools.local_tools.ensure_authorized()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_chat_reports_whether_the_inline_reply_was_delivered(temp_app_root: Path) -> None:
    app     = XiaoQingApp(temp_app_root)
    reports = []

    async def report_delivery(report, context):
        reports.append((report, context.plugin_name))

    _install_service(
        app, "xiaoqing_agent", "agent.report_delivery", "xiaoqing_chat", report_delivery
    )

    def chat(services: frozenset[str]):
        return _plugin_context_for(
            app,
            "xiaoqing_chat",
            user_id       = OTHER,
            principal     = _principal(app, OTHER),
            uses_services = services,
        ).capabilities.agent_turn

    bridge = chat(frozenset({"agent.handle_turn", "agent.report_delivery"}))
    await bridge.report_delivery("abc123", False)
    await bridge.report_delivery("abc123", None)
    assert reports == [
        ({"task_id": "abc123", "delivered": False}, "xiaoqing_agent"),
        ({"task_id": "abc123", "delivered": None}, "xiaoqing_agent"),
    ]
    with pytest.raises(TypeError):
        await bridge.report_delivery("abc123", "yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await bridge.report_delivery("  ", True)

    # 没有声明回报这条桥的调用方：什么也不做。
    await chat(frozenset({"agent.handle_turn"})).report_delivery("abc123", True)
    assert len(reports) == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_keeps_the_receipt_and_separates_each_call(
    temp_app_root: Path,
) -> None:
    """命令的发送回执原样交回调用方；每次调用的请求 ID 不同，同一次调用的重试相同。"""

    from core.delivery import CommandOutcome, DeliveryReceipt, DeliverySegments

    app      = XiaoQingApp(temp_app_root)
    requests = []
    receipt  = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: None,
        rollback         = lambda: None,
        unknown          = lambda: None,
    )

    async def pick(_command, _args, _event, context):
        requests.append(context.request_id)
        return DeliverySegments(
            [{"type": "text", "data": {"text": "图"}}],
            receipt,
            outcome=CommandOutcome("accepted"),
        )

    _register_command(app, "twitter", "twimg", pick)
    runner = _plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = _principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner

    first = await runner.run("/twimg", operation_id="task1-1")
    assert isinstance(first, DeliverySegments) and first.delivery_receipt is receipt
    assert first.command_outcome == CommandOutcome("accepted")
    await runner.run("/twimg", operation_id="task1-2")
    await runner.run("/twimg", operation_id="task1-2")
    await runner.run("/twimg")
    assert requests[0] != requests[1] and requests[1] == requests[2]
    assert requests[3] not in requests[:3]
    with pytest.raises(ValueError):
        await runner.run("/twimg", operation_id="bad id")
