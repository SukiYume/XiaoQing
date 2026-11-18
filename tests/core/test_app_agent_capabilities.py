"""小青 agent 能力：和对应命令同一套权限，每次调用重新确认，并按固定服务契约转发。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import tests.helpers.app_test_support as _fixture_support
from core.models import PluginManifest
from core.session import Session
from tests.helpers.app_test_support import (
    PROBE,
    UNUSED_COMMAND_SOURCE,
    XiaoQingApp,
    command_declaration,
    load_test_plugin,
    plugin_context_for,
    service_declaration,
    user_principal,
)

temp_app_root                               = _fixture_support.temp_app_root
_isolate_process_global_plugin_import_state = (
    _fixture_support._isolate_process_global_plugin_import_state
)

ADMIN = 12345
OTHER = 55555

AGENT_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle_turn(turn, context):
    PROBE.log("handle_turn").append((turn, context))
    return PROBE.next_result("handle_turn")

async def report_delivery(report, context):
    PROBE.log("report_delivery").append((report, context.plugin_name))
"""

CODEX_SOURCE = (
    UNUSED_COMMAND_SOURCE
    + """
from tests.helpers.plugin_probe import PROBE

async def agent_task_service(request, context):
    PROBE.log("codex").append((request, context.plugin_name))
    return {"status": "done", "final_text": "ok", "images": []}
"""
)

SSH_SOURCE = (
    UNUSED_COMMAND_SOURCE
    + """
from tests.helpers.plugin_probe import PROBE

async def agent_command_service(request, context):
    PROBE.log("ssh").append((request, context.plugin_name))
    if request["op"] == "list":
        return [{"name": "lab"}]
    return {"ok": True, "exit_code": 0, "output": "hi"}
"""
)

# 长命令的服务把远端工作的 Future 交回给 Core：Future 由测试创建，经记录器共享。
SSH_REMOTE_SOURCE = (
    UNUSED_COMMAND_SOURCE
    + """
from tests.helpers.plugin_probe import PROBE

async def agent_command_service(request, context):
    PROBE.log("ssh").append(request["op"])
    if request["op"] == "list":
        return [{"name": "lab"}]
    return PROBE.slots["remote"]
"""
)

# agent 工具对应的三个插件和它们的命令触发词。
TOOL_TRIGGERS = {"shell": "shell", "qingssh": "ssh", "codex": "codex"}


async def _load_tool_plugin(
    app: XiaoQingApp,
    plugin: str,
    source: str = UNUSED_COMMAND_SOURCE,
    *,
    permission: str            = "bot_admin",
    services: tuple[dict, ...] = (),
    concurrency: str           = "parallel",
) -> None:
    """装入 agent 工具对应的插件：它的 ``/shell``、``/ssh``、``/codex`` 声明决定工具给谁。"""

    await load_test_plugin(
        app,
        plugin,
        source,
        commands=[command_declaration(TOOL_TRIGGERS[plugin], permission=permission)],
        services    = list(services),
        concurrency = concurrency,
    )


async def _publish_tool_commands(app: XiaoQingApp, **permissions: str) -> None:
    """装入只声明命令的工具插件；默认与生产 manifest 一致，都只给 Bot 管理员。"""

    for plugin in TOOL_TRIGGERS:
        await _load_tool_plugin(app, plugin, permission=permissions.get(plugin, "bot_admin"))


ECHO_ARGS_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("echo").append((command, args, event, context))
    return [{"type": "text", "data": {"text": f"echo:{args}"}}]
"""

RECORD_COMMAND_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("ran").append(command)
    return [{"type": "text", "data": {"text": f"ran:{command}"}}]
"""

GIFT_EVENT_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("gift").append((args, event))
    return [{"type": "text", "data": {"text": "ok"}}]
"""

GIFT_REQUEST_TEXTS_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("request_texts").append(context.request_texts)
    return [{"type": "text", "data": {"text": "ok"}}]
"""

LEDGER_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("ledger").append(args)
    return [{"type": "text", "data": {"text": "账本"}}]
"""

# /guess 开一个交互会话并停在等待回复；/guess2 先看一眼当前能看到的会话再开。
GUESS_SOURCE = """
from core.delivery import CommandOutcome, DeliverySegments
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    if command == "guess2":
        PROBE.log("guess2_session").append(await context.get_session())
        await context.create_session({"step": 1})
        return [{"type": "text", "data": {"text": "游戏开始，请输入数字"}}]
    await context.create_session({"step": 1})
    return DeliverySegments(
        [{"type": "text", "data": {"text": "游戏开始，请输入数字"}}],
        outcome=CommandOutcome("incomplete", confirmation_command="/guess accept game-123"),
    )
"""

# /connect 占用一份资源并开会话；会话被收回时由 close_session 释放资源。
RESOURCE_SOURCE = """
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("resources").append("connection")
    await context.create_session({"server": "lab"})
    return [{"type": "text", "data": {"text": "已连接 lab"}}]

async def close_session(event, context, session):
    closed = (session.plugin_name, session.get("server"), context.current_user_id)
    PROBE.log("closed").append(closed)
    PROBE.log("resources").remove("connection")
"""

RECEIPT_SOURCE = """
from core.delivery import CommandOutcome, DeliverySegments
from tests.helpers.plugin_probe import PROBE

async def handle(command, args, event, context):
    PROBE.log("request_id").append(context.request_id)
    return DeliverySegments(
        [{"type": "text", "data": {"text": "图"}}],
        PROBE.slots["receipt"],
        outcome=CommandOutcome("accepted"),
    )
"""


async def _install_agent(app: XiaoQingApp, *services: str) -> None:
    """装入 xiaoqing_agent；``services`` 是它导出给 xiaoqing_chat 的服务名。"""

    await load_test_plugin(
        app,
        "xiaoqing_agent",
        AGENT_SOURCE,
        services=[
            service_declaration(name, name.split(".")[1], ("xiaoqing_chat",)) for name in services
        ],
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_capabilities_follow_the_matching_command_permissions(
    temp_app_root: Path,
) -> None:
    """对话和命令执行给每个用户（命令自己再查权限）；Codex、SSH 按 /codex、/ssh 的声明授权。"""

    app = XiaoQingApp(temp_app_root)
    await _publish_tool_commands(app)
    for user_id, admin in ((ADMIN, True), (OTHER, False)):
        chat = plugin_context_for(
            app,
            "xiaoqing_chat",
            user_id       = user_id,
            principal     = user_principal(app, user_id),
            uses_services = frozenset({"agent.handle_turn"}),
        )
        agent = plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id               = user_id,
            principal             = user_principal(app, user_id),
            manifest_capabilities = frozenset({"command_runner"}),
            uses_services         = frozenset({"codex.agent_task", "qingssh.agent_command"}),
        )
        assert chat.capabilities.agent_turn is not None
        assert agent.capabilities.command_runner is not None
        assert (agent.capabilities.codex_agent is not None) is admin
        assert (agent.capabilities.ssh_agent is not None) is admin

    undeclared = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id   = ADMIN,
        principal = user_principal(app, ADMIN),
    )
    assert undeclared.capabilities.agent_turn is None
    assert undeclared.capabilities.command_runner is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_turn_reaches_agent_with_the_speakers_principal(
    temp_app_root: Path,
) -> None:
    app = XiaoQingApp(temp_app_root)
    principal = user_principal(app, ADMIN, group_id=777)
    seen = PROBE.log("handle_turn")
    PROBE.script(
        "handle_turn",
        {"handled": True, "segments": [{"type": "text", "data": {"text": "ok"}}]},
    )
    await _install_agent(app, "agent.handle_turn")
    chat = plugin_context_for(
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
    app = XiaoQingApp(temp_app_root)
    PROBE.script("handle_turn", None, "not a mapping")
    await _install_agent(app, "agent.handle_turn")
    capability = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = ADMIN,
        principal     = user_principal(app, ADMIN),
        uses_services = frozenset({"agent.handle_turn"}),
    ).capabilities.agent_turn
    assert await capability.handle_turn({"text": "在吗"}) is None
    with pytest.raises(TypeError):
        await capability.handle_turn({"text": "在吗"})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_turn_reports_missing_agent_plugin_as_unavailable(temp_app_root: Path) -> None:
    app        = XiaoQingApp(temp_app_root)
    capability = plugin_context_for(
        app,
        "xiaoqing_chat",
        user_id       = ADMIN,
        principal     = user_principal(app, ADMIN),
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
    codex = PROBE.log("codex")
    ssh   = PROBE.log("ssh")
    await _load_tool_plugin(
        app,
        "codex",
        CODEX_SOURCE,
        services=(
            service_declaration("codex.agent_task", "agent_task_service", ("xiaoqing_agent",)),
        ),
    )
    await _load_tool_plugin(
        app,
        "qingssh",
        SSH_SOURCE,
        services=(
            service_declaration(
                "qingssh.agent_command", "agent_command_service", ("xiaoqing_agent",)
            ),
        ),
    )
    agent = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id       = ADMIN,
        principal     = user_principal(app, ADMIN),
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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_executes_published_commands_as_the_speaker(
    temp_app_root: Path,
) -> None:
    app   = XiaoQingApp(temp_app_root)
    calls = PROBE.log("echo")
    await load_test_plugin(app, "echo", ECHO_ARGS_SOURCE, commands=[command_declaration("echo")])
    # agent 自己的命令不能经执行器调用：Core 在到达处理函数之前就拒绝。
    await load_test_plugin(
        app,
        "xiaoqing_agent",
        UNUSED_COMMAND_SOURCE,
        commands=[command_declaration("agent")],
    )
    runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id  = ADMIN,
        group_id = 888,
        principal=user_principal(app, ADMIN, group_id=888),
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

    app = XiaoQingApp(temp_app_root)
    ran = PROBE.log("ran")
    for plugin, trigger, permission in (
        ("shell", "shell", "bot_admin"),
        ("qingpet", "petadmin", "group_admin"),
        ("echo", "echo", "public"),
    ):
        await load_test_plugin(
            app,
            plugin,
            RECORD_COMMAND_SOURCE,
            commands=[command_declaration(trigger, permission=permission)],
        )

    def runner(user_id: int, role: str = ""):
        return plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id  = user_id,
            group_id = 888,
            principal=user_principal(app, user_id, group_id=888, role=role),
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
    events = PROBE.log("gift")
    await load_test_plugin(
        app, "qingpet", GIFT_EVENT_SOURCE, commands=[command_declaration("gift")]
    )
    runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = user_principal(app, ADMIN),
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
async def test_command_runner_hands_initiator_words_to_the_command_context(
    temp_app_root: Path,
) -> None:
    """发起人原话随执行器的调用进入插件上下文；普通消息事件带同名字段也读不到。"""

    from core.interfaces import (
        COMMAND_REQUEST_TEXTS_EVENT_KEY,
        COMMAND_RUNNER_EVENT_KEY,
        MAX_COMMAND_REQUEST_TEXT_CHARS,
        command_request_texts,
    )

    app      = XiaoQingApp(temp_app_root)
    received = PROBE.log("request_texts")
    await load_test_plugin(
        app, "qingpet", GIFT_REQUEST_TEXTS_SOURCE, commands=[command_declaration("gift")]
    )
    runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = user_principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner

    long_text = "长" * (MAX_COMMAND_REQUEST_TEXT_CHARS + 10)
    await runner.run("/gift 小鱼干", request_texts=(" 给它小鱼干 ", "  ", long_text))
    await runner.run("/gift 小鱼干")
    assert received == [("给它小鱼干", "长" * MAX_COMMAND_REQUEST_TEXT_CHARS), ()]
    with pytest.raises(ValueError, match="request texts"):
        await runner.run("/gift 小鱼干", request_texts=("ok", 1))

    forged = {COMMAND_REQUEST_TEXTS_EVENT_KEY: ["伪造的原话"]}
    assert command_request_texts(forged) == ()
    assert command_request_texts({**forged, COMMAND_RUNNER_EVENT_KEY: "xiaoqing_agent"}) == (
        "伪造的原话",
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_explains_context_restricted_commands(temp_app_root: Path) -> None:
    app   = XiaoQingApp(temp_app_root)
    calls = PROBE.log("ledger")
    await load_test_plugin(
        app,
        "pendo",
        LEDGER_SOURCE,
        commands=[command_declaration("ledger", contexts=("private",))],
    )
    group_runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id  = ADMIN,
        group_id = 888,
        principal=user_principal(app, ADMIN, group_id=888),
        manifest_capabilities=frozenset({"command_runner"}),
    ).capabilities.command_runner
    reply = await group_runner.run("/ledger list 2026-09")
    assert reply == [
        {"type": "text", "data": {"text": "/ledger 只能在私聊里使用，当前是群聊，这里不能执行。"}}
    ]
    assert calls == []

    private_runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = user_principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner
    assert (await private_runner.run("/ledger list 2026-09"))[0]["data"]["text"] == "账本"
    assert calls == ["list 2026-09"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_closes_sessions_opened_by_the_command(temp_app_root: Path) -> None:
    app = XiaoQingApp(temp_app_root)
    await load_test_plugin(
        app,
        "guess_number",
        GUESS_SOURCE,
        commands=[command_declaration("guess"), command_declaration("guess2")],
    )
    runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = user_principal(app, ADMIN),
        manifest_capabilities = frozenset({"command_runner"}),
    ).capabilities.command_runner

    reply = await runner.run("/guess")
    assert reply.command_outcome.state == "incomplete"
    assert reply.command_outcome.confirmation_command == "/guess accept game-123"
    assert [segment["data"]["text"] for segment in reply] == [
        "游戏开始，请输入数字",
        "（这条命令停在了等待回复的交互步骤，操作没有完成，会话已结束。请改用一次性写法，比如补全参数或使用跳过确认的选项；需要发起人决定的，先把情况告诉发起人。）",
    ]
    assert await app.session_manager.peek(ADMIN, None) is None

    existing = await app.session_manager.create(ADMIN, None, "qingssh", initial_data={"k": 1})
    assert isinstance(existing, Session)
    await runner.run("/guess2")

    kept = await app.session_manager.peek(ADMIN, None)
    assert kept is not None
    assert (kept.session_id, kept.plugin_name, kept.data) == (
        existing.session_id,
        "qingssh",
        {"k": 1},
    )
    # 命令看不到用户正在进行的交互，也不会把它当成自己的会话继续。
    assert PROBE.log("guess2_session") == [None]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_command_runner_lets_the_plugin_release_a_discarded_sessions_resources(
    temp_app_root: Path,
) -> None:
    """命令开了交互会话就被收回时，和用户发“退出”一样交给插件的 close_session 收尾。"""

    app       = XiaoQingApp(temp_app_root)
    resources = PROBE.log("resources")
    closed    = PROBE.log("closed")
    await load_test_plugin(
        app,
        "resource_plugin",
        RESOURCE_SOURCE,
        commands    = [command_declaration("connect")],
        concurrency = "sequential",
    )
    own = await app.session_manager.create(ADMIN, None, "qingssh", initial_data={"k": 1})
    runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = user_principal(app, ADMIN),
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
    app  = XiaoQingApp(temp_app_root)
    loop = asyncio.get_running_loop()
    remote = PROBE.slots["remote"] = loop.create_future()
    requests = PROBE.log("ssh")
    await _load_tool_plugin(
        app,
        "qingssh",
        SSH_REMOTE_SOURCE,
        services=(
            service_declaration(
                "qingssh.agent_command", "agent_command_service", ("xiaoqing_agent",)
            ),
        ),
        concurrency="sequential",
    )
    ssh = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id       = ADMIN,
        principal     = user_principal(app, ADMIN),
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
        remote = PROBE.slots["remote"] = loop.create_future()
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
                    "name"    : "agent.handle_turn",
                    "callback": "handle_turn",
                    "callers" : ["xiaoqing_chat"],
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
                    "name"    : "agent.handle_turn",
                    "callback": "handle_turn",
                    "callers" : ["xiaoqing_chat"],
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
        "life.turn_context",
        "life.export_memes",
        "memory.record",
        "memory.recall",
        "memory.search",
        "memory.forget",
    }
    agent = load("xiaoqing_agent")
    assert set(agent.uses_services) == {
        "codex.agent_task",
        "qingssh.agent_command",
        "memory.record",
        "memory.recall",
    }
    assert [service.name for service in agent.services] == [
        "agent.handle_turn",
        "agent.report_delivery",
    ]
    assert agent.capabilities == ["command_runner", "local_tools"]
    assert [service.name for service in load("codex").services] == [
        "codex.enqueue_arxiv_summary",
        "codex.travel_postcards",
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
    await _publish_tool_commands(app)

    def agent(user_id: int, capabilities: frozenset[str]):
        return plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id               = user_id,
            principal             = user_principal(app, user_id),
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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_agent_tools_change_with_the_command_declarations(temp_app_root: Path) -> None:
    """只有一处规则：改了 /shell、/ssh、/codex 的声明，agent 的工具跟着变。"""

    app = XiaoQingApp(temp_app_root)

    def agent():
        return plugin_context_for(
            app,
            "xiaoqing_agent",
            user_id               = OTHER,
            principal             = user_principal(app, OTHER),
            manifest_capabilities = frozenset({"command_runner", "local_tools"}),
            uses_services         = frozenset({"codex.agent_task", "qingssh.agent_command"}),
        ).capabilities

    # 命令没有发布（插件没加载）时谁都拿不到对应工具。
    assert agent().local_tools is None and agent().ssh_agent is None

    await _publish_tool_commands(app, shell="public")
    tools = agent()
    assert tools.local_tools is not None
    assert tools.ssh_agent is None and tools.codex_agent is None

    # 声明改回只给 Bot 管理员后，已经发出的能力下一次调用就被拒绝。
    await _publish_tool_commands(app)
    with pytest.raises(PermissionError):
        tools.local_tools.ensure_authorized()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_chat_reports_whether_the_inline_reply_was_delivered(temp_app_root: Path) -> None:
    app     = XiaoQingApp(temp_app_root)
    reports = PROBE.log("report_delivery")
    await _install_agent(app, "agent.report_delivery")

    def chat(services: frozenset[str]):
        return plugin_context_for(
            app,
            "xiaoqing_chat",
            user_id       = OTHER,
            principal     = user_principal(app, OTHER),
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
    requests = PROBE.log("request_id")
    receipt = PROBE.slots["receipt"] = DeliveryReceipt(
        expected_actions = 1,
        commit           = lambda: None,
        rollback         = lambda: None,
        unknown          = lambda: None,
    )
    await load_test_plugin(app, "twitter", RECEIPT_SOURCE, commands=[command_declaration("twimg")])
    runner = plugin_context_for(
        app,
        "xiaoqing_agent",
        user_id               = ADMIN,
        principal             = user_principal(app, ADMIN),
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
