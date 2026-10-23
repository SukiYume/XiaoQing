"""agent 工具：真实执行 Python/Shell、文件读写、计划与进度、XiaoQing 命令、SSH 与 Codex。"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.router import build_command_catalog_node
from plugins.xiaoqing_agent.config import AgentConfig
from plugins.xiaoqing_agent.shells import resolve_shell
from plugins.xiaoqing_agent.tasks import AgentTask, ChatRef
from plugins.xiaoqing_agent.tools import (
    ToolEnv,
    ToolError,
    build_tools,
    codex_session_label,
    execute_tool,
    parse_arguments,
)
from tests.helpers.asyncio_tools import run_with_subprocess_loop
from tests.helpers.xiaoqing_agent_test_support import ADMIN_ID, GROUP_ID, FakeAgentContext

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _env(
    tmp_path: Path,
    context: Any | None = None,
    *,
    config: AgentConfig | None = None,
    remaining: float           = 600.0,
    admin: bool                = True,
) -> ToolEnv:
    task = AgentTask("abc123", ChatRef(ADMIN_ID, None), "目标", "原话", "chat", admin=admin)
    workspace = tmp_path / "workspace"
    out_dir   = workspace / "out" / task.task_id
    out_dir.mkdir(parents=True)
    sent: list[tuple[str, list[str]]] = []

    async def send_progress(text: str, files: list[str]) -> bool:
        sent.append((text, files))
        return True

    send_progress.sent = sent  # type: ignore[attr-defined]
    return ToolEnv(
        context=context or FakeAgentContext(tmp_path / "data"),
        config=config or AgentConfig(enabled=True),
        task          = task,
        workspace     = workspace,
        out_dir       = out_dir,
        python        = sys.executable,
        shell         = resolve_shell(""),
        deadline      = time.monotonic() + remaining,
        send_progress = send_progress,
    )


def _tools(env: ToolEnv, plugins: tuple[str, ...] = ()) -> dict:
    return build_tools(env, loaded_plugins=frozenset(plugins))


def test_python_tool_computes_and_reports_new_files(tmp_path: Path) -> None:
    env   = _env(tmp_path)
    tools = _tools(env)

    async def scenario() -> None:
        result = await execute_tool(
            tools,
            env,
            "run_python",
            {"code": "print(2 ** 100)\nopen('out/abc123/data.csv', 'w').write('a,b\\n1,2\\n')"},
        )
        assert "exit_code=0" in result.content
        assert str(2**100) in result.content
        assert "data.csv" in result.content
        script = env.workspace / ".agent" / "scripts" / "abc123-01.py"
        assert script.is_file()
        assert ".agent" not in result.content.split("新增或修改的文件:")[-1]

        failed = await execute_tool(tools, env, "run_python", {"code": "raise SystemExit('boom')"})
        assert "exit_code=1" in failed.content and "boom" in failed.content
        assert env.script_counter == 2

    run_with_subprocess_loop(scenario)


@pytest.mark.slow
def test_python_tool_draws_a_png_with_matplotlib(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    env   = _env(tmp_path)
    tools = _tools(env)
    code  = (
        "import matplotlib.pyplot as plt, numpy as np\n"
        "x = np.linspace(0, 6.28, 50)\n"
        "plt.plot(x, np.sin(x))\n"
        "plt.savefig('out/abc123/sin.png')\n"
        "print('saved')\n"
    )

    async def scenario() -> None:
        result = await execute_tool(
            tools, env, "run_python", {"code": code, "timeout_seconds": 120}
        )
        assert "saved" in result.content, result.content
        image = env.out_dir / "sin.png"
        assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    run_with_subprocess_loop(scenario)


def test_python_tool_timeout_is_reported(tmp_path: Path) -> None:
    env   = _env(tmp_path)
    tools = _tools(env)

    async def scenario() -> None:
        result = await execute_tool(
            tools,
            env,
            "run_python",
            {"code": "import time\ntime.sleep(30)", "timeout_seconds": 1},
        )
        assert "超时" in result.content

    run_with_subprocess_loop(scenario)


def test_shell_tool_runs_in_the_workspace(tmp_path: Path) -> None:
    env   = _env(tmp_path)
    tools = _tools(env)
    (env.workspace / "marker.txt").write_text("hello", encoding="utf-8")

    async def scenario() -> None:
        listing = "Get-ChildItem -Name" if env.shell.label == "PowerShell" else "ls"
        result  = await execute_tool(tools, env, "run_shell", {"command": listing})
        assert "exit_code=0" in result.content
        assert "marker.txt" in result.content

    run_with_subprocess_loop(scenario)


@pytest.mark.asyncio
async def test_tool_timeout_respects_task_budget(tmp_path: Path) -> None:
    env = _env(tmp_path, remaining=2.0)
    tools  = _tools(env)
    result = await execute_tool(tools, env, "run_python", {"code": "print(1)"})
    assert result.content.startswith("错误：") and "final_answer" in result.content


@pytest.mark.asyncio
async def test_file_tools_round_trip_and_page_long_files(tmp_path: Path) -> None:
    config = AgentConfig(enabled=True, max_tool_output_chars=1000)
    env = _env(tmp_path, config=config)
    tools = _tools(env)

    written = await execute_tool(
        tools, env, "write_file", {"path": "notes/a.txt", "content": "x" * 1500}
    )
    assert "已写入" in written.content
    first = await execute_tool(tools, env, "read_file", {"path": "notes/a.txt"})
    assert first.content.startswith("x" * 1000) and "offset" in first.content
    rest = await execute_tool(tools, env, "read_file", {"path": "notes/a.txt", "offset": 1000})
    assert rest.content == "x" * 500

    (env.workspace / "blob.bin").write_bytes(b"\x00\x01binary")
    binary = await execute_tool(tools, env, "read_file", {"path": "blob.bin"})
    assert "二进制" in binary.content
    missing = await execute_tool(tools, env, "read_file", {"path": "nope.txt"})
    assert missing.content.startswith("错误：")
    bad = await execute_tool(tools, env, "write_file", {"path": "a.txt", "content": 3})
    assert bad.content.startswith("错误：")


@pytest.mark.asyncio
async def test_plan_progress_wait_and_final_answer(tmp_path: Path, monkeypatch) -> None:
    env = _env(tmp_path, remaining=3600)
    tools = _tools(env)

    plan = await execute_tool(
        tools,
        env,
        "update_plan",
        {
            "steps": [
                {"step": "下载数据", "status": "done"},
                {"step": "画图", "status": "in_progress"},
            ]
        },
    )
    assert plan.content == "计划已更新"
    assert env.task.plan[1] == {"step": "画图", "status": "in_progress"}
    assert env.task.activity == "画图"
    assert (await execute_tool(tools, env, "update_plan", {"steps": []})).content.startswith("错误")

    first  = await execute_tool(tools, env, "send_message", {"text": "下好了，开始画图"})
    second = await execute_tool(tools, env, "send_message", {"text": "还在画"})
    assert first.content == "已发送"
    assert "太近" in second.content
    assert env.send_progress.sent == [("下好了，开始画图", [])]  # type: ignore[attr-defined]

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("plugins.xiaoqing_agent.tools.asyncio.sleep", fake_sleep)
    waited = await execute_tool(tools, env, "wait", {"seconds": 9999})
    assert sleeps == [600.0] and "600" in waited.content

    missing = await execute_tool(
        tools, env, "final_answer", {"text": "好了", "files": ["out/abc123/x.png"]}
    )
    assert missing.final is None and "不存在" in missing.content
    (env.out_dir / "x.png").write_bytes(PNG_BYTES)
    done = await execute_tool(
        tools, env, "final_answer", {"text": "好了", "files": ["out/abc123/x.png"]}
    )
    assert done.final is not None
    assert done.final.text == "好了"
    assert done.final.files == [str(env.out_dir / "x.png")]


@pytest.mark.asyncio
async def test_argument_errors_are_returned_to_the_model(tmp_path: Path) -> None:
    env   = _env(tmp_path)
    tools = _tools(env)
    assert "合法 JSON" in (await execute_tool(tools, env, "run_python", "{oops")).content
    assert "没有名为" in (await execute_tool(tools, env, "rm_rf", {})).content
    assert "code" in (await execute_tool(tools, env, "run_python", {})).content
    assert parse_arguments("") == {}
    with pytest.raises(ToolError):
        parse_arguments("[1, 2]")


def _catalog() -> tuple[Any, ...]:
    pendo = build_command_catalog_node(
        "pendo",
        {
            "name": "pendo",
            "triggers": ["pendo"],
            "help": "个人时间与信息管理",
            "usage": "/pendo <subcommand>",
            "contexts": ["private"],
            "subcommands": [
                {
                    "name": "todo",
                    "help": "待办",
                    "usage": "/pendo todo <add|list>",
                    "examples": ["/pendo todo add 写周报 plan:2026-08-01"],
                },
                {"name": "event", "help": "日程", "usage": "/pendo event <add|list>"},
            ],
        },
        root=True,
    )
    agent = build_command_catalog_node(
        "xiaoqing_agent",
        {"name": "agent", "triggers": ["agent"], "help": "agent", "usage": "/agent run"},
        root=True,
    )
    return (pendo, agent)


@pytest.mark.asyncio
async def test_xiaoqing_command_tools_search_catalog_and_convert_replies(tmp_path: Path) -> None:
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(PNG_BYTES)
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=[
                {"type": "text", "data": {"text": "今天 2 个待办"}},
                {"type": "image", "data": {"file": image_path.as_uri()}},
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(PNG_BYTES).decode()},
                },
                {"type": "image", "data": {"file": "https://example.com/x.png"}},
                {"type": "face", "data": {"id": "1"}},
            ]
        )
    )
    context = FakeAgentContext(tmp_path / "data", catalog=_catalog(), command_runner=runner)
    env   = _env(tmp_path, context)
    tools = _tools(env)

    roots = await execute_tool(tools, env, "xiaoqing_commands", {})
    assert "/pendo <subcommand>" in roots.content and "/agent" not in roots.content
    todo = await execute_tool(tools, env, "xiaoqing_commands", {"query": "待办"})
    assert "/pendo todo" in todo.content and "/pendo event" not in todo.content
    assert "例：/pendo todo add 写周报 plan:2026-08-01" in todo.content
    assert "例：" not in roots.content
    assert "/pendo <subcommand> — 个人时间与信息管理（仅私聊）" in roots.content
    by_example = await execute_tool(tools, env, "xiaoqing_commands", {"query": "写周报"})
    assert "/pendo todo <add|list>" in by_example.content
    none = await execute_tool(tools, env, "xiaoqing_commands", {"query": "不存在的东西"})
    assert "没有匹配" in none.content

    reply = await execute_tool(tools, env, "xiaoqing_command", {"command": "/pendo todo list"})
    runner.run.assert_awaited_once_with("/pendo todo list", sender_name="", operation_id="abc123-1")
    assert reply.content.startswith("今天 2 个待办")
    assert "[图片 https://example.com/x.png]" in reply.content and "[face]" in reply.content
    assert str(image_path) in reply.content
    saved = env.out_dir / "command-image-1.png"
    assert saved.read_bytes() == PNG_BYTES and str(saved) in reply.content

    # 同一任务里再调用一次命令，返回的图片不能覆盖上一张。
    other_png               = PNG_BYTES + b"second"
    runner.run.return_value = [
        {"type": "image", "data": {"file": "base64://" + base64.b64encode(other_png).decode()}},
    ]
    again  = await execute_tool(tools, env, "xiaoqing_command", {"command": "/pendo todo list"})
    second = env.out_dir / "command-image-2.png"
    assert str(second) in again.content
    assert saved.read_bytes() == PNG_BYTES and second.read_bytes() == other_png


def test_optional_tools_follow_capabilities_and_loaded_plugins(tmp_path: Path) -> None:
    bare = _env(tmp_path / "bare")
    assert set(_tools(bare)) == {
        "run_python",
        "run_shell",
        "read_file",
        "write_file",
        "update_plan",
        "send_message",
        "wait",
        "final_answer",
    }
    context = FakeAgentContext(
        tmp_path / "data",
        command_runner=SimpleNamespace(run=AsyncMock()),
        codex_agent=SimpleNamespace(run_task=AsyncMock()),
        ssh_agent=SimpleNamespace(list_servers=AsyncMock(), run=AsyncMock()),
    )
    full = _env(tmp_path / "full", context)
    assert {"xiaoqing_commands", "xiaoqing_command"} <= set(_tools(full))
    assert "codex_run" not in _tools(full) and "ssh_run" not in _tools(full)
    loaded = _tools(full, ("codex", "qingssh"))
    assert {"codex_run", "ssh_servers", "ssh_run"} <= set(loaded)
    for tool in loaded.values():
        schema = tool.schema()
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_ssh_tools_format_remote_results(tmp_path: Path) -> None:
    ssh = SimpleNamespace(
        list_servers=AsyncMock(return_value=[{"name": "lab", "host": "10.0.0.5"}]),
        run=AsyncMock(
            side_effect=[
                {"ok": True, "exit_code": 0, "output": "up 3 days", "timed_out": False},
                {
                    "ok": False,
                    "exit_code": None,
                    "output": "",
                    "timed_out": True,
                    "truncated": True,
                },
            ]
        ),
    )
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", ssh_agent=ssh))
    tools = _tools(env, ("qingssh",))

    assert "10.0.0.5" in (await execute_tool(tools, env, "ssh_servers", {})).content
    ok = await execute_tool(tools, env, "ssh_run", {"server": "lab", "command": "uptime"})
    assert ok.content.startswith("exit_code=0") and "up 3 days" in ok.content
    assert ssh.run.await_args.kwargs["server"] == "lab"
    assert ssh.run.await_args.kwargs["timeout_seconds"] == 120.0
    slow = await execute_tool(
        tools,
        env,
        "ssh_run",
        {"server": "lab", "command": "sleep 999", "timeout_seconds": 5},
    )
    assert "超时" in slow.content and "截断" in slow.content


@pytest.mark.asyncio
async def test_codex_tool_uses_a_per_chat_session_and_returns_images(tmp_path: Path) -> None:
    codex = SimpleNamespace(
        run_task=AsyncMock(
            side_effect=[
                {
                    "status": "done",
                    "exit_code": 0,
                    "cwd": "C:/work",
                    "final_text": "脚本写好了",
                    "images": ["C:/codex/img.png"],
                },
                {"status": "error", "error": "会话名不合法"},
            ]
        )
    )
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", codex_agent=codex))
    tools  = _tools(env, ("codex",))
    result = await execute_tool(
        tools, env, "codex_run", {"prompt": "写个爬虫", "timeout_minutes": 2}
    )
    assert "脚本写好了" in result.content and "C:/codex/img.png" in result.content
    kwargs = codex.run_task.await_args.kwargs
    assert kwargs["session"] == codex_session_label(env.task) == f"agent-u{ADMIN_ID}"
    assert kwargs["timeout_seconds"] == 120.0
    assert kwargs["cwd"] is None
    failed = await execute_tool(tools, env, "codex_run", {"prompt": "x"})
    assert failed.content == "错误：会话名不合法"


@pytest.mark.asyncio
async def test_unavailable_services_become_tool_errors(tmp_path: Path) -> None:
    ssh = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("plugin service is unavailable")))
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", ssh_agent=ssh))
    tools  = _tools(env, ("qingssh",))
    result = await execute_tool(tools, env, "ssh_run", {"server": "lab", "command": "ls"})
    assert result.content.startswith("错误：RuntimeError")


@pytest.mark.asyncio
async def test_command_voice_video_and_files_are_exposed_as_local_paths(tmp_path: Path) -> None:
    voice = tmp_path / "tts.mp3"
    voice.write_bytes(b"mp3")
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=[
                {"type": "record", "data": {"file": voice.as_uri()}},
                {
                    "type": "video",
                    "data": {"file": "base64://" + base64.b64encode(b"mp4").decode()},
                },
                {"type": "file", "data": {"file": str(tmp_path / "export.md")}},
            ]
        )
    )
    context = FakeAgentContext(tmp_path / "data", command_runner=runner)
    env   = _env(tmp_path, context)
    reply = await execute_tool(_tools(env), env, "xiaoqing_command", {"command": "/语音 晚安"})
    assert f"命令返回的语音（可放进 final_answer.files 发出去）: {voice}" in reply.content
    saved = env.out_dir / "command-video-1.mp4"
    assert saved.read_bytes() == b"mp4" and str(saved) in reply.content
    assert (
        f"命令返回的文件（可放进 final_answer.files 发出去）: {tmp_path / 'export.md'}"
        in reply.content
    )


@pytest.mark.asyncio
async def test_read_file_pages_without_loading_the_whole_file(tmp_path: Path, monkeypatch) -> None:
    import plugins.xiaoqing_agent.tools as agent_tools

    config = AgentConfig(enabled=True, max_tool_output_chars=1000)
    env = _env(tmp_path, config=config)
    tools = _tools(env)
    big   = env.workspace / "big.log"
    big.write_text("".join(f"{index:07d}\n" for index in range(200_000)), encoding="utf-8")
    monkeypatch.setattr(agent_tools, "_READ_SKIP_CHARS", 4096)

    def forbid(*_args, **_kwargs):
        raise AssertionError("read_file must not load the whole file")

    monkeypatch.setattr(Path, "read_text", forbid)
    middle = await execute_tool(tools, env, "read_file", {"path": "big.log", "offset": 800_000})
    assert middle.content.startswith("0100000\n")
    assert "用 offset=801000 继续读" in middle.content
    beyond = await execute_tool(tools, env, "read_file", {"path": "big.log", "offset": 99_000_000})
    assert "超出文件末尾" in beyond.content
    end = await execute_tool(tools, env, "read_file", {"path": "big.log", "offset": 1_600_000})
    assert "文件末尾" in end.content


def _catalog_for_permissions() -> tuple:
    return (
        build_command_catalog_node(
            "apod",
            {"name": "apod", "triggers": ["apod"], "help": "每日天文图", "usage": "/apod"},
            root=True,
        ),
        build_command_catalog_node(
            "shell",
            {
                "name": "shell",
                "triggers": ["shell"],
                "help": "在本机执行命令",
                "usage": "/shell <命令>",
                "permission": "bot_admin",
            },
            root=True,
        ),
        build_command_catalog_node(
            "qingpet",
            {
                "name": "qingpet",
                "triggers": ["宠物"],
                "help": "宠物养成",
                "usage": "/宠物 <子命令>",
                "subcommands": [
                    {"name": "feed", "help": "喂食宠物", "usage": "/宠物 feed"},
                    {
                        "name": "admin",
                        "help": "宠物群管理",
                        "usage": "/宠物 admin",
                        "permission": "group_admin",
                    },
                ],
            },
            root=True,
        ),
    )


@pytest.mark.asyncio
async def test_members_get_command_tools_only_and_send_only_what_commands_returned(
    tmp_path: Path,
) -> None:
    picture = tmp_path / "plugin_data" / "apod.png"
    picture.parent.mkdir()
    picture.write_bytes(PNG_BYTES)
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=[
                {"type": "text", "data": {"text": "今日天文图"}},
                {"type": "image", "data": {"file": picture.as_uri()}},
            ]
        )
    )
    context = FakeAgentContext(tmp_path / "data", is_admin=False, command_runner=runner)
    env = _env(tmp_path, context, admin=False)
    tools = _tools(env, ("qingssh", "codex"))
    assert set(tools) == {
        "update_plan",
        "send_message",
        "wait",
        "final_answer",
        "xiaoqing_commands",
        "xiaoqing_command",
    }

    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    for name in ("final_answer", "send_message"):
        refused = await execute_tool(tools, env, name, {"text": "给你", "files": [str(secret)]})
        assert refused.final is None and "不是这次命令返回的文件" in refused.content
    assert env.send_progress.sent == []  # type: ignore[attr-defined]

    reply = await execute_tool(tools, env, "xiaoqing_command", {"command": "/apod"})
    assert str(picture) in reply.content
    sent = await execute_tool(
        tools, env, "final_answer", {"text": "今天的图", "files": [str(picture)]}
    )
    assert sent.final is not None and sent.final.files == [str(picture)]
    made = env.out_dir / "command-image-1.png"
    made.write_bytes(PNG_BYTES)
    kept = await execute_tool(tools, env, "final_answer", {"text": "x", "files": [str(made)]})
    assert kept.final is not None


@pytest.mark.asyncio
async def test_command_list_only_shows_what_the_requester_may_run(tmp_path: Path) -> None:
    from plugins.xiaoqing_agent.prompts import capability_summary

    catalog = _catalog_for_permissions()
    runner = SimpleNamespace(run=AsyncMock(return_value=[]))

    async def listing(**context_options: Any) -> tuple[str, str, str]:
        context = FakeAgentContext(
            tmp_path / "data", catalog=catalog, command_runner=runner, **context_options
        )
        env   = _env(tmp_path / str(len(context_options)), context)
        tools = _tools(env)
        top   = (await execute_tool(tools, env, "xiaoqing_commands", {})).content
        pet   = (await execute_tool(tools, env, "xiaoqing_commands", {"query": "宠物"})).content
        return top, pet, capability_summary(catalog, context.principal)

    top, pet, summary = await listing(is_admin=False, group_id=GROUP_ID)
    assert "/apod" in top and "/宠物" in top and "/shell" not in top
    assert "/宠物 feed" in pet and "/宠物 admin" not in pet
    assert "/shell" not in summary and "/apod" in summary

    _top, pet, _summary = await listing(is_admin=False, group_id=GROUP_ID, group_role="admin")
    assert "/宠物 admin" in pet

    top, pet, summary = await listing(is_admin=True)
    assert "/shell" in top and "/宠物 admin" in pet and "/shell" in summary


@pytest.mark.asyncio
async def test_local_tools_stop_working_as_soon_as_the_admin_is_revoked(tmp_path: Path) -> None:
    """本机工具每次调用都向 Core 复核：撤掉 Bot 管理员后同一任务里立即失效。"""

    context = FakeAgentContext(tmp_path / "data")
    env     = _env(tmp_path, context)
    tools   = _tools(env)
    assert {"run_python", "run_shell", "read_file", "write_file"} <= set(tools)
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    assert "token" in (await execute_tool(tools, env, "read_file", {"path": str(secret)})).content

    context.capabilities.local_tools.allowed = False  # type: ignore[union-attr]
    for name, args in (
        ("read_file", {"path": str(secret)}),
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("run_shell", {"command": "echo hi"}),
        ("run_python", {"code": "print(1)"}),
    ):
        result = await execute_tool(tools, env, name, args)
        assert "不是 Bot 管理员" in result.content, name
    assert not (env.workspace / "a.txt").exists()

    # 能发出的文件也随之收窄到这次任务的产出。
    refused = await execute_tool(
        tools, env, "final_answer", {"text": "给你", "files": [str(secret)]}
    )
    assert refused.final is None and "不是这次命令返回的文件" in refused.content
    produced = env.out_dir / "plot.png"
    produced.write_bytes(PNG_BYTES)
    kept = await execute_tool(tools, env, "final_answer", {"text": "图", "files": [str(produced)]})
    assert kept.final is not None


def test_local_tools_need_the_core_capability_even_for_an_admin_task(tmp_path: Path) -> None:
    context = FakeAgentContext(tmp_path / "data", local_tools=None)
    tools = _tools(_env(tmp_path, context, admin=True))
    assert not {"run_python", "run_shell", "read_file", "write_file"} & set(tools)
