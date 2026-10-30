"""agent 工具：真实执行 Python/Shell、文件读写、计划与进度、XiaoQing 命令、SSH 与 Codex。"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.delivery import CommandOutcome, DeliverySegments
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


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", [None, "unknown", 1])
async def test_command_requires_valid_delivery_before_execution(tmp_path, delivery):
    runner = SimpleNamespace(run=AsyncMock())
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", command_runner=runner))
    args = {"command": "/catalog"}
    if delivery is not None:
        args["delivery"] = delivery
    result = await execute_tool(_tools(env), env, "xiaoqing_command", args)
    assert result.state == "error" and "delivery" in result.content
    runner.run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery,expected_calls", [("reply", 1), ("all_pages", 2), ("continue", 1)]
)
async def test_command_delivery_uses_explicit_pages_and_result_text(
    tmp_path, delivery, expected_calls
):
    runner = SimpleNamespace(
        run=AsyncMock(
            side_effect=[
                DeliverySegments(
                    [{"type": "text", "data": {"text": "第一项。查看详情：/catalog detail"}}],
                    outcome=CommandOutcome(
                        "completed", next_command="/catalog page 2", result_text="第一项", page=1
                    ),
                ),
                DeliverySegments(
                    [{"type": "text", "data": {"text": "第二项。继续试试其他功能"}}],
                    outcome=CommandOutcome("completed", result_text="第二项", has_more=False),
                ),
            ]
        )
    )
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", command_runner=runner))
    result = await execute_tool(
        _tools(env), env, "xiaoqing_command", {"command": "/catalog", "delivery": delivery}
    )
    assert runner.run.await_count == expected_calls
    assert "查看详情" not in result.content and "试试其他功能" not in result.content
    assert (result.final is None) == (delivery == "continue")
    if delivery == "all_pages":
        assert "第一项" in result.final.text and "第二项" in result.final.text
        assert "后续分页：无" in result.final.text


@pytest.fixture
def ledger_pages(tmp_path, request):
    from plugins.pendo.handlers.ledger import LedgerHandler
    from plugins.pendo.services.db import Database

    db   = Database(str(tmp_path / "pagination.db"))
    base = {
        "owner_id"            : "pagination-user",
        "type"                : "ledger",
        "amount"              : 25,
        "amount_cents"        : 2500,
        "transaction_type"    : "transfer",
        "currency"            : "CNY",
        "ledger_category"     : "测试 分类",
        "ledger_date"         : "2026-03-15",
        "account_name"        : "测试 账户",
        "counter_account_name": "目标 账户",
        "merchant"            : "测试 商户",
        "created_at"          : "2026-03-15T12:00:00",
        "updated_at"          : "2026-03-15T12:00:00",
    }
    for index in range(getattr(request, "param", 21)):
        db.insert_item({**base, "id": f"entry{index:03d}", "title": f"记录{index:03d}"})
    for index, overrides in enumerate(
        [
            {"owner_id": "other-user"},
            {"ledger_date": "2026-04-01"},
            {"transaction_type": "expense"},
            {"ledger_category": "其他"},
            {"account_name": "其他"},
            {"counter_account_name": "其他"},
            {"merchant": "其他"},
            {"amount_cents": 1000, "amount": 10},
            {"amount_cents": 10000, "amount": 100},
        ]
    ):
        db.insert_item({**base, **overrides, "id": f"excluded{index}", "title": f"排除{index}"})
    yield LedgerHandler(db)
    db.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_pages", [11, 21], indirect=True)
@pytest.mark.parametrize("delivery", ["all_pages", "reply", "continue"])
async def test_real_pendo_filtered_pagination(tmp_path, ledger_pages, delivery):
    from plugins.pendo.main import _format_result

    calls     = []
    responses = []

    async def run(command, **_kwargs):
        calls.append(command)
        response = await ledger_pages.handle(
            "pagination-user", command.removeprefix("/pendo ledger "), SimpleNamespace()
        )
        responses.append(response)
        return _format_result(response)

    env = _env(
        tmp_path, FakeAgentContext(tmp_path / "data", command_runner=SimpleNamespace(run=run))
    )
    filters = '2026-03 type:transfer account:"测试 账户" to:“目标 账户” merchant:"测试 商户" cat:"测试 分类" amount:20..30 page:1'
    result = await execute_tool(
        _tools(env),
        env,
        "xiaoqing_command",
        {
            "command" : "/pendo ledger list " + filters,
            "delivery": delivery,
        },
    )
    total = sum(
        item.title.startswith("记录") for item in ledger_pages.db.get_all_items("pagination-user")
    )
    assert len(calls) == ((total + 9) // 10 if delivery == "all_pages" else 1)
    expected = range(total) if delivery == "all_pages" else range(total - 10, total)
    for index in expected:
        assert result.content.count(f"记录{index:03d}") == 1
    assert "排除" not in result.content
    assert (result.final is None) == (delivery == "continue")
    assert ("分页已取齐" in result.content) == (delivery == "all_pages")
    original = ledger_pages._parse_list_filters(filters)
    for page, command in enumerate(calls[1:], 2):
        parsed = ledger_pages._parse_list_filters(command.removeprefix("/pendo ledger list "))
        assert parsed == replace(original, page=page, range_text="2026-03-01..2026-03-31")
        assert responses[page - 2]["outcome"].next_command == command
    assert responses[-1]["outcome"].has_more == (delivery != "all_pages")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected,complete",
    [
        ("2026-04", 1, True),
        ("2026-05", 0, True),
        ("2026-03 page:3", 8, False),
        ("2026-03 all", 28, True),
    ],
)
async def test_real_pendo_single_empty_all_and_middle_page(
    tmp_path, ledger_pages, query, expected, complete
):
    from plugins.pendo.main import _format_result

    async def run(command, **_kwargs):
        return _format_result(
            await ledger_pages.handle(
                "pagination-user", command.removeprefix("/pendo ledger "), SimpleNamespace()
            )
        )

    env = _env(
        tmp_path, FakeAgentContext(tmp_path / "data", command_runner=SimpleNamespace(run=run))
    )
    result = await execute_tool(
        _tools(env),
        env,
        "xiaoqing_command",
        {
            "command" : "/pendo ledger list " + query,
            "delivery": "all_pages",
        },
    )
    assert len(re.findall(r"记录\d{3}|排除\d", result.content)) == expected
    assert ("分页已取齐" in result.content) == complete
    if not complete:
        assert "前序页尚未取齐" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "exception", "budget", "steps", "truncated"])
async def test_real_pendo_partial_results_survive_stop(tmp_path, ledger_pages, failure):
    from plugins.pendo.main import _format_result

    calls = []

    async def run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 2 and failure == "exception":
            raise OSError("offline executor failed")
        args = command.removeprefix("/pendo ledger ")
        if len(calls) == 2 and failure == "error":
            args = "list page:invalid"
        response = await ledger_pages.handle("pagination-user", args, SimpleNamespace())
        if failure == "budget":
            env.deadline = time.monotonic()
        return _format_result(response)

    env = _env(
        tmp_path, FakeAgentContext(tmp_path / "data", command_runner=SimpleNamespace(run=run))
    )
    if failure == "steps":
        env.config = replace(env.config, max_steps=1)
    if failure == "truncated":
        env.config = replace(env.config, max_tool_output_chars=300)
    result = await execute_tool(
        _tools(env),
        env,
        "xiaoqing_command",
        {
            "command" : "/pendo ledger list 2026-03",
            "delivery": "all_pages",
        },
    )
    assert "尚未取齐" in result.content and "分页已取齐" not in result.content
    if failure == "truncated":
        assert "截断" in result.content
    else:
        assert "记录020" in result.content
        assert len(calls) == (2 if failure in {"error", "exception"} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_more", [None, True, False])
async def test_missing_navigation_never_implies_complete(tmp_path, has_more):
    from plugins.pendo.main import _format_result

    response = {"status": "success", "message": "本页成功"}
    if has_more is not None:
        response["outcome"] = CommandOutcome("completed", has_more=has_more)
    runner = SimpleNamespace(run=AsyncMock(return_value=_format_result(response)))
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", command_runner=runner))
    result = await execute_tool(
        _tools(env),
        env,
        "xiaoqing_command",
        {
            "command" : "/catalog",
            "delivery": "all_pages",
        },
    )
    assert "分页已取齐" not in result.content
    if has_more is not False:
        assert "后续分页：无" not in result.content
    assert ("尚未取齐" if has_more is True else "完整性未知") in result.content


@pytest.mark.asyncio
async def test_real_pendo_navigation_preserves_extra_and_resolves_relative_dates(ledger_pages):
    from plugins.pendo.main import _format_result
    from plugins.pendo.utils.time_utils import get_user_local_wall_time

    now = await get_user_local_wall_time("pagination-user", ledger_pages.db)
    for item in ledger_pages.db.get_all_items("pagination-user"):
        ledger_pages.db.update_item(
            item.id, {"ledger_date": now.strftime("%Y-%m-%d")}, owner_id="pagination-user"
        )
    response = await ledger_pages.handle(
        "pagination-user", "list today ex page:1", SimpleNamespace()
    )
    outcome = _format_result(response).command_outcome
    assert outcome.has_more is True
    args   = outcome.next_command.removeprefix("/pendo ledger list ")
    parsed = ledger_pages._parse_list_filters(args)
    assert parsed.show_extra and parsed.page == 2
    assert parsed.range_text == f"{now:%Y-%m-%d}..{now:%Y-%m-%d}"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cycle", "budget", "update"])
async def test_pagination_preserves_partial_results_and_stops_on_failure_or_update(
    tmp_path, failure
):
    env   = _env(tmp_path, FakeAgentContext(tmp_path / "data"))
    calls = []

    async def command(value, **_kwargs):
        calls.append(value)
        if len(calls) == 2:
            if failure == "update":
                env.task.notes.append("只需要第一项")
            if failure == "budget":
                env.deadline = time.monotonic()
        state        = "error" if failure == "error" and len(calls) == 2 else "completed"
        next_command = "/catalog" if failure == "cycle" else f"/catalog page {len(calls) + 1}"
        return DeliverySegments(
            [{"type": "text", "data": {"text": f"第{len(calls)}次：{state}"}}],
            outcome=CommandOutcome(state, next_command=next_command),
        )

    env.context.capabilities = replace(
        env.context.capabilities, command_runner=SimpleNamespace(run=command)
    )
    result = await execute_tool(
        _tools(env), env, "xiaoqing_command", {"command": "/catalog", "delivery": "all_pages"}
    )
    assert len(calls) <= 2
    assert (result.final is not None) == (failure == "error")
    assert "第1次" in result.content
    if failure in {"cycle", "budget"}:
        assert result.state == "blocked" and "尚未取齐" in result.content
    elif failure == "error":
        assert result.state == "error"
        assert result.final.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["reply", "continue"])
async def test_delivery_preserves_command_attachment_without_copying_path(tmp_path, delivery):
    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=[
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(PNG_BYTES).decode()},
                },
            ]
        )
    )
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", command_runner=runner))
    result = await execute_tool(
        _tools(env), env, "xiaoqing_command", {"command": "/picture", "delivery": delivery}
    )
    if delivery == "continue":
        assert result.final is None
        result = await execute_tool(_tools(env), env, "final_answer", {"text": "图片给你了"})
    assert result.final is not None and len(result.final.files) == 1
    assert Path(result.final.files[0]).read_bytes() == PNG_BYTES
    text_only = await execute_tool(
        _tools(env), env, "final_answer", {"text": "只发文字", "files": []}
    )
    assert text_only.final.files == []


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_unchanged_result_reports_no_progress_before_stopping(tmp_path, completed):
    from tests.core.test_command_catalog import _load_catalog

    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=DeliverySegments(
                [{"type": "text", "data": {"text": "两篇论文及链接"}}],
                outcome=CommandOutcome("completed") if completed else None,
            )
        )
    )
    context = FakeAgentContext(
        tmp_path / "data", catalog=_load_catalog().roots, command_runner=runner
    )
    env   = _env(tmp_path, context)
    tools = _tools(env)
    first = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/paper search FRB"}
    )
    second = await execute_tool(
        tools,
        env,
        "xiaoqing_command",
        {"delivery": "continue", "command": "/paper search fast radio burst"},
    )
    third = await execute_tool(
        tools,
        env,
        "xiaoqing_command",
        {"delivery": "continue", "command": "/paper search repeater"},
    )
    assert first.progress and first.content.startswith("两篇论文及链接")
    assert not second.progress and "未取得新信息" in second.content
    assert second.state == "ok" and third.state == "blocked"
    assert (third.final is not None) == completed
    if completed:
        assert third.final.status == "done"


@pytest.mark.asyncio
async def test_repeated_service_failure_closes_same_command_across_page_changes(tmp_path):
    from tests.core.test_command_catalog import _load_catalog

    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=DeliverySegments(
                [{"type": "text", "data": {"text": "连接超时"}}], outcome=CommandOutcome("error")
            )
        )
    )
    env = _env(
        tmp_path,
        FakeAgentContext(tmp_path / "data", catalog=_load_catalog().roots, command_runner=runner),
    )
    for page in (2, 2, 3):
        result = await execute_tool(
            _tools(env),
            env,
            "xiaoqing_command",
            {"command": f"/color search 红 --page {page}", "delivery": "continue"},
        )
    assert result.state == "blocked"
    assert runner.run.await_count == 2


@pytest.mark.asyncio
async def test_denied_catalog_and_command_report_blocked_without_running(tmp_path):
    from tests.core.test_command_catalog import _load_catalog

    runner = SimpleNamespace(run=AsyncMock())
    context = FakeAgentContext(
        tmp_path / "data", catalog=_load_catalog().roots, command_runner=runner, is_admin=False
    )
    env   = _env(tmp_path, context)
    tools = _tools(env)
    for query in ("ssh", "qingssh", "shell", "codex", "问答库"):
        result = await execute_tool(tools, env, "xiaoqing_commands", {"query": query})
        assert result.state == "blocked" and "权限" in result.content
    denied = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/ssh list"}
    )
    assert denied.state == "blocked"
    runner.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_help_is_bounded_and_new_queries_remain_available(tmp_path):
    from tests.core.test_command_catalog import _load_catalog

    env = _env(
        tmp_path,
        FakeAgentContext(
            tmp_path / "data",
            catalog=_load_catalog().roots,
            command_runner=SimpleNamespace(run=AsyncMock()),
        ),
    )
    tools   = _tools(env)
    results = [
        await execute_tool(tools, env, "xiaoqing_commands", {"query": "github"}) for _ in range(3)
    ]
    assert [result.state for result in results] == ["ok", "ok", "blocked"]
    assert results[0].progress and not results[1].progress
    new_query = await execute_tool(tools, env, "xiaoqing_commands", {"query": "color"})
    assert new_query.state == "ok" and new_query.progress


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "error", "incomplete"])
async def test_command_outcome_is_visible_to_model_without_text_prefix(tmp_path, state):
    from core.delivery import CommandOutcome, DeliverySegments

    runner = SimpleNamespace(
        run=AsyncMock(
            return_value=DeliverySegments(
                [{"type": "text", "data": {"text": "服务返回的说明"}}],
                outcome=CommandOutcome(state),
            )
        )
    )
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", command_runner=runner))
    result = await execute_tool(
        _tools(env), env, "xiaoqing_command", {"delivery": "continue", "command": "/demo"}
    )
    assert f"执行状态：{state}" in result.content
    assert result.state == ("ok" if state == "completed" else "error")


@pytest.mark.asyncio
async def test_background_acceptance_closes_only_its_command(tmp_path):
    from core.delivery import CommandOutcome, DeliverySegments
    from tests.core.test_command_catalog import _load_catalog

    runner = SimpleNamespace(
        run=AsyncMock(
            side_effect=[
                DeliverySegments([], outcome=CommandOutcome("accepted")),
                [{"type": "text", "data": {"text": "已有缓存图片"}}],
            ]
        )
    )
    context = FakeAgentContext(
        tmp_path / "data", catalog=_load_catalog().roots, command_runner=runner
    )
    env     = _env(tmp_path, context)
    tools   = _tools(env)
    started = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/tw_fetch"}
    )
    repeated = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/tw_fetch"}
    )
    cached = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/twimg"}
    )
    assert started.state == "accepted" and repeated.state == "blocked"
    assert cached.content == "已有缓存图片" and cached.state == "ok"
    assert runner.run.await_count == 2


@pytest.mark.asyncio
async def test_ledger_tool_and_legacy_command_always_attach_initiator_source(tmp_path):
    from tests.core.test_command_catalog import _load_catalog

    runner = SimpleNamespace(
        run=AsyncMock(return_value=[{"type": "text", "data": {"text": "记账成功"}}])
    )
    context = FakeAgentContext(
        tmp_path / "data", catalog=_load_catalog().roots, command_runner=runner
    )
    env                   = _env(tmp_path, context)
    env.task.request_text = "记账 会员99.47 服务 信用卡"
    tools = build_tools(env, loaded_plugins=frozenset({"pendo"}))
    assert "source_text" not in tools["pendo_ledger_add"].parameters["properties"]
    await execute_tool(
        tools,
        env,
        "pendo_ledger_add",
        {"amount_cents": 9947, "title": "会员", "source_text": "伪造来源"},
    )
    await execute_tool(
        tools,
        env,
        "xiaoqing_command",
        {"delivery": "continue", "command": "/pendo ledger add 99.47 会员 cat:服务 account:信用卡"},
    )
    assert runner.run.await_count == 2
    for call in runner.run.await_args_list:
        command = call.args[0]
        assert command.startswith("/pendo ledger record ")
        payload = json.loads(command.removeprefix("/pendo ledger record "))
        assert payload["source_text"] == env.task.request_text
        assert payload["amount_cents"] == 9947


@pytest.mark.asyncio
async def test_catalog_combines_keywords_and_paginates(tmp_path):
    context = FakeAgentContext(
        tmp_path / "data", catalog=_catalog(), command_runner=SimpleNamespace(run=AsyncMock())
    )
    env    = _env(tmp_path, context)
    result = await execute_tool(_tools(env), env, "xiaoqing_commands", {"query": "pendo 待办"})
    assert "/pendo todo" in result.content
    assert "/pendo event" not in result.content
    nodes = tuple(
        build_command_catalog_node(
            "demo",
            {
                "name"    : f"item{i}",
                "triggers": [f"item{i}"],
                "usage"   : f"/item{i}",
                "help"    : "demo",
            },
            root=True,
        )
        for i in range(45)
    )
    env.context._catalog = nodes
    first = await execute_tool(_tools(env), env, "xiaoqing_commands", {"query": "demo"})
    second = await execute_tool(_tools(env), env, "xiaoqing_commands", {"query": "demo", "page": 2})
    assert "page=2" in first.content and "/item40" not in first.content
    assert "/item40" in second.content and "/item44" in second.content


@pytest.mark.asyncio
async def test_unknown_middle_word_returns_complete_candidate_commands(tmp_path):
    from plugins.pendo.main import _local_catalog_root

    context = FakeAgentContext(
        tmp_path / "data",
        catalog        = (_local_catalog_root(),),
        command_runner = SimpleNamespace(),
    )
    env    = _env(tmp_path, context)
    result = await execute_tool(
        _tools(env), env, "xiaoqing_commands", {"query": "pendo schedule add"}
    )
    assert "完整路径未匹配" in result.content
    assert "/pendo event add" in result.content and "准点提醒" in result.content
    assert "/pendo ledger add" in result.content and "currency:" in result.content
    result = await execute_tool(
        _tools(env),
        env,
        "xiaoqing_commands",
        {"query": "pendo event reminders ack 确认"},
    )
    assert "/pendo event reminders confirm" in result.content and "标记为已确认" in result.content
    assert "pendo.pendo.confirm" not in result.content


@pytest.mark.asyncio
async def test_catalog_keeps_named_plugin_scope_and_parent_purpose(tmp_path):
    from tests.core.test_command_catalog import _load_catalog

    context = FakeAgentContext(
        tmp_path / "data", catalog=_load_catalog().roots, command_runner=SimpleNamespace()
    )
    env    = _env(tmp_path, context)
    result = await execute_tool(_tools(env), env, "xiaoqing_commands", {"query": "pendo todo show"})
    assert "/pendo todo view" in result.content
    assert "bot_core.help.search" not in result.content
    result = await execute_tool(_tools(env), env, "xiaoqing_commands", {"query": "pendo todo add"})
    assert "日历预约使用 event" in result.content


@pytest.mark.asyncio
async def test_catalog_pages_keep_each_command_contract_whole(tmp_path):
    help_text = "参数说明" * 180 + " 最后一个参数也完整保留"
    nodes     = tuple(
        build_command_catalog_node(
            "demo",
            {
                "name"    : f"entry{i}",
                "triggers": [f"entry{i}"],
                "usage"   : f"/entry{i}",
                "help"    : help_text,
                "examples": [f"/entry{i} one", f"/entry{i} two", f"/entry{i} three"],
            },
            root=True,
        )
        for i in range(2)
    )
    context = FakeAgentContext(tmp_path / "data", catalog=nodes, command_runner=SimpleNamespace())
    env = _env(tmp_path, context)
    env.config = AgentConfig(max_tool_output_chars=1000)
    tools = _tools(env)
    for i in range(2):
        result = await execute_tool(
            tools, env, "xiaoqing_commands", {"query": "demo", "page": i + 1}
        )
        assert help_text in result.content
        assert f"/entry{i} three" in result.content
        assert f"/entry{1 - i}" not in result.content


@pytest.mark.asyncio
async def test_inline_progress_is_held_and_background_progress_can_be_sent(tmp_path):
    env                    = _env(tmp_path)
    tools                  = _tools(env)
    env.task.inline_waiter = asyncio.get_running_loop().create_future()
    held                   = await execute_tool(tools, env, "send_message", {"text": "数据读完了，正在计算"})
    assert "当轮正在等待" in held.content and env.task.progress_sent == 0
    assert env.send_progress.sent == []
    env.task.inline_waiter = None
    sent                   = await execute_tool(tools, env, "send_message", {"text": "计算完了，正在导出图片"})
    assert sent.content == "已发送" and env.task.progress_sent == 1
    assert env.send_progress.sent == [("计算完了，正在导出图片", [])]


@pytest.mark.asyncio
async def test_read_image_uses_vision_and_rejects_invalid_input(tmp_path):
    from PIL import Image

    from tests.helpers.xiaoqing_agent_test_support import FakeAI

    ai = FakeAI([{"choices": [{"message": {"content": "截止日期：11月13日"}}]}])
    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", ai=ai))
    path = env.workspace / "notice.png"
    Image.new("RGB", (4, 4)).save(path)
    result = await execute_tool(_tools(env), env, "read_image", {"path": str(path)})
    assert "11月13日" in result.content
    route, messages, _ = ai.calls[0]
    assert route == "vision"
    assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    path.write_bytes(b"invalid image")
    result = await execute_tool(_tools(env), env, "read_image", {"path": str(path)})
    assert result.content.startswith("错误") and len(ai.calls) == 1
    member = _env(
        tmp_path / "member", FakeAgentContext(tmp_path / "member-data", is_admin=False), admin=False
    )
    assert "read_image" not in _tools(member)


@pytest.mark.asyncio
async def test_read_image_failure_preserves_environment(tmp_path):
    from PIL import Image

    from core.ai import AIError
    from tests.helpers.xiaoqing_agent_test_support import FakeAI

    env = _env(tmp_path, FakeAgentContext(tmp_path / "data", ai=FakeAI([AIError("offline")])))
    path = env.workspace / "notice.png"
    Image.new("RGB", (4, 4)).save(path)
    result = await execute_tool(_tools(env), env, "read_image", {"path": str(path)})
    assert "补充文字" in result.content and "保留当前运行环境" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin,group,available", [(True, None, True), (False, None, False), (True, GROUP_ID, False)]
)
async def test_xiaoai_structured_tools_use_command_permissions_and_original_request(
    tmp_path, admin, group, available
):
    node = build_command_catalog_node(
        "xiaoai",
        {
            "name"      : "xiaoai",
            "triggers"  : ["米家"],
            "help"      : "米家设备",
            "permission": "bot_admin",
            "contexts"  : ["private"],
        },
        root=True,
    )
    runner = SimpleNamespace(run=AsyncMock(return_value=[{"type": "text", "data": {"text": "ok"}}]))
    context = FakeAgentContext(
        tmp_path / "data", catalog=(node,), command_runner=runner, is_admin=admin, group_id=group
    )
    env                   = _env(tmp_path, context)
    env.task.request_text = "出两份粮"
    env.task.goal         = "模型改写后的任务"
    tools = build_tools(env, loaded_plugins=frozenset({"xiaoqing_agent", "xiaoai"}))
    assert ("xiaoai_execute" in tools) == available
    if not available:
        return
    await execute_tool(tools, env, "xiaoai_devices", {})
    await execute_tool(tools, env, "xiaoai_capabilities", {"device": "d1", "page": 2})
    ops = [{"device": "d1", "capability": "a2.1", "op": "action", "args": [20]}]
    await execute_tool(tools, env, "xiaoai_execute", {"operations": ops})
    calls = runner.run.await_args_list
    assert calls[0].args[0] == "/米家 数据"
    assert calls[1].args[0] == "/米家 接口 d1 2"
    payload = json.loads(calls[2].args[0].removeprefix("/米家 调用 "))
    assert payload == {"operations": ops, "original_request": "出两份粮"}
    assert calls[2].kwargs["operation_id"] == "abc123-3"


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
        assert failed.state == "error" and not failed.progress
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
            "name"       : "pendo",
            "triggers"   : ["pendo"],
            "help"       : "个人时间与信息管理",
            "usage"      : "/pendo <subcommand>",
            "contexts"   : ["private"],
            "subcommands": [
                {
                    "name"    : "todo",
                    "help"    : "待办",
                    "usage"   : "/pendo todo <add|list>",
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

    reply = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/pendo todo list"}
    )
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
    again = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/pendo todo list"}
    )
    second = env.out_dir / "command-image-2.png"
    assert str(second) in again.content
    assert saved.read_bytes() == PNG_BYTES and second.read_bytes() == other_png


def test_optional_tools_follow_capabilities_and_loaded_plugins(tmp_path: Path) -> None:
    bare = _env(tmp_path / "bare")
    assert set(_tools(bare)) == {
        "run_python",
        "run_shell",
        "read_file",
        "read_image",
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
                    "ok"       : False,
                    "exit_code": None,
                    "output"   : "",
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
                    "status"    : "done",
                    "exit_code" : 0,
                    "cwd"       : "C:/work",
                    "final_text": "脚本写好了",
                    "images"    : ["C:/codex/img.png"],
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
    reply = await execute_tool(
        _tools(env), env, "xiaoqing_command", {"delivery": "continue", "command": "/语音 晚安"}
    )
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
                "name"      : "shell",
                "triggers"  : ["shell"],
                "help"      : "在本机执行命令",
                "usage"     : "/shell <命令>",
                "permission": "bot_admin",
            },
            root=True,
        ),
        build_command_catalog_node(
            "qingpet",
            {
                "name"       : "qingpet",
                "triggers"   : ["宠物"],
                "help"       : "宠物养成",
                "usage"      : "/宠物 <子命令>",
                "subcommands": [
                    {"name": "feed", "help": "喂食宠物", "usage": "/宠物 feed"},
                    {
                        "name"      : "admin",
                        "help"      : "宠物群管理",
                        "usage"     : "/宠物 admin",
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

    reply = await execute_tool(
        tools, env, "xiaoqing_command", {"delivery": "continue", "command": "/apod"}
    )
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
    assert "/shell" in summary and "/apod" in summary
    assert "当前身份无权使用" in summary

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
