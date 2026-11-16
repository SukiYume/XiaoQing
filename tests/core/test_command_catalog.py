"""全插件命令目录契约，以及统一 ``/event`` 入站路由门禁。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from aiohttp import ClientSession

from core.dispatcher import AdjustableSemaphore, Dispatcher
from core.interfaces import PluginPrincipal
from core.models import PluginManifest
from core.router import (
    CommandCatalogNode,
    CommandInvocation,
    CommandRouter,
    CommandSpec,
    build_command_catalog_node,
    resolve_catalog_invocation,
)
from tests.helpers.command_catalog_test_support import (
    PLUGIN_MANIFESTS,
    CatalogFixture,
    load_production_catalog,
)
from tests.helpers.server_test_support import StandaloneInboundServer


def _resolve_example(
    router: CommandRouter,
    example: str,
) -> tuple[CommandSpec, CommandInvocation]:
    resolved = router.resolve(example.strip().lstrip("/"))
    assert resolved is not None, f"目录样例未命中任何顶层命令: {example}"
    spec, args = resolved
    assert spec.catalog is not None
    return spec, resolve_catalog_invocation(spec.catalog, args)


def test_every_plugin_exposes_a_complete_recursive_command_contract() -> None:
    """每个用户命令都必须有稳定码、用法以及正常/错误样例。"""

    fixture = load_production_catalog()
    assert len(fixture.manifests) == len(PLUGIN_MANIFESTS)
    assert fixture.manifests

    commandless_plugins = {manifest.name for manifest in fixture.manifests if not manifest.commands}
    assert commandless_plugins == {"url_parser"}, "仅被动 URL 监听器允许没有用户命令"

    nodes = tuple(node for root in fixture.roots for node in root.walk())
    codes = [node.code for node in nodes]
    assert len(codes) == len(set(codes)), "稳定命令码必须全局唯一"
    assert len(nodes) > len(fixture.roots), "目录不能退化成只有顶层入口"

    for root in fixture.roots:
        assert root.path == (root.name,)
        for node in root.walk():
            assert node.usage.strip(), f"{node.code} 缺少 usage"
            assert node.examples, f"{node.code} 缺少正常样例"
            assert node.invalid_examples, f"{node.code} 缺少错误样例"

            for example in node.examples:
                spec, invocation = _resolve_example(fixture.router, example)
                assert spec.catalog is root, f"{node.code} 的样例路由到了其他根命令"
                assert node.code in {selected.code for selected in invocation.chain}, (
                    f"{node.code} 的正常样例没有经过该目录节点: {example}"
                )

            for example in node.invalid_examples:
                spec, invocation = _resolve_example(fixture.router, example)
                assert spec.catalog is root, f"{node.code} 的错误样例路由到了其他根命令"
                assert invocation.root is root


def test_sensitive_command_surfaces_declare_their_boundaries() -> None:
    """个人数据入口由发布目录声明私聊边界；本机与服务器执行入口群里也能用，但只给 Bot 管理员。"""

    fixture = load_production_catalog()
    for root in fixture.roots:
        if root.plugin == "pendo":
            assert root.contexts == ("private",), root.code
        if root.plugin in {"shell", "jupyter", "qingssh", "codex"}:
            assert root.permission == "bot_admin", root.code
            assert root.contexts == ("private", "group"), root.code

    nodes = {node.code: node for root in fixture.roots for node in root.walk()}
    # Minecraft 群里所有人都能聊天和看在线玩家；连接、断开和原样服务器命令只给 Bot 管理员。
    for code in ("minecraft.mc.say", "minecraft.mc.tell", "minecraft.mc.list", "minecraft.mc"):
        assert nodes[code].permission == "public", code
        assert nodes[code].contexts == ("private", "group"), code
    for code in (
        "minecraft.mc.connect",
        "minecraft.mc.disconnect",
        "minecraft.mc.run",
        "minecraft.mcconnect",
        "minecraft.mcdisconnect",
    ):
        assert nodes[code].permission == "bot_admin", code

    private_paper_prefixes = (
        "ads_paper.paper.note",
        "ads_paper.paper.writing",
        "ads_paper.paper.topics",
        "ads_paper.paper.deadline",
        "ads_paper.paper.daily",
        "ads_paper.paper.ref_add",
        "ads_paper.paper.refs",
    )
    for code, node in nodes.items():
        if code.startswith(private_paper_prefixes):
            assert node.contexts == ("private",), code

    for code in (
        "ads_paper.paper.search",
        "ads_paper.paper.author",
        "ads_paper.paper.cite",
        "ads_paper.paper.cite-network",
        "ads_paper.paper.related",
        "ads_paper.paper.summarize",
    ):
        assert nodes[code].contexts == ("private", "group"), code

    for code in ("bot_core.set_secret", "bot_core.get_secret"):
        assert nodes[code].contexts == ("private",), code


def test_help_plugin_and_stable_code_queries_cover_the_same_catalog() -> None:
    """``/help`` 的插件查询和稳定码查询不得漏掉 manifest 中的节点。"""

    from plugins.bot_core.main import _catalog_page, _select_catalog_nodes

    fixture = load_production_catalog()
    for manifest in fixture.manifests:
        expected = tuple(
            node for root in fixture.roots if root.plugin == manifest.name for node in root.walk()
        )
        if not expected:
            continue

        selected = _select_catalog_nodes(fixture.roots, manifest.name)
        assert [node.code for node in selected] == [node.code for node in expected]

        paged: list[CommandCatalogNode] = []
        _first_page, total_pages = _catalog_page(selected, 1)
        for page in range(1, total_pages + 1):
            page_nodes, page_count = _catalog_page(selected, page)
            assert page_count == total_pages
            paged.extend(page_nodes)
        assert [node.code for node in paged] == [node.code for node in expected]

        for node in expected:
            exact = _select_catalog_nodes(fixture.roots, node.code)
            assert exact
            assert exact[0].code == node.code
            assert [item.code for item in exact] == [item.code for item in node.walk()]


@pytest.mark.parametrize(
    "query,code",
    [
        ("pendo 记账 add", "pendo.pendo.ledger.add"),
        ("米家 接口", "xiaoai.xiaoai.接口"),
        ("/米家/调用", "xiaoai.xiaoai.调用"),
    ],
)
def test_help_resolves_aliases_at_every_level(query: str, code: str) -> None:
    from plugins.bot_core.main import _find_exact_catalog_nodes

    nodes = _find_exact_catalog_nodes(load_production_catalog().roots, query)
    assert [node.code for node in nodes] == [code]


def test_agent_exact_help_preserves_every_published_parameter_and_example() -> None:
    from plugins.xiaoqing_agent.tools import catalog_lines

    fixture = load_production_catalog()
    context = SimpleNamespace(get_command_catalog=lambda: fixture.roots, principal=None)
    for root in fixture.roots:
        if root.plugin in {"xiaoqing_agent", "xiaoqing_chat"}:
            continue
        for node in root.walk():
            text = "\n".join(catalog_lines(context, " ".join(node.path)))
            assert node.usage in text and node.help_text in text, node.code
            assert all(example in text for example in node.examples), node.code
    text = "\n".join(catalog_lines(context, "pendo 记账 add"))
    assert "currency:" in text and "remark:" in text and "cat:" in text
    assert "pendo.pendo.ledger.edit" not in text


def test_mobile_help_progressively_discloses_real_pendo_catalog() -> None:
    """人类帮助只显示当前层；叶节点详情和 JSON 仍保留完整契约。"""

    from plugins.bot_core.main import (
        HELP_MOBILE_LINE_WIDTH,
        _display_width,
        _find_exact_catalog_nodes,
        _find_plugin_roots,
        _format_branch_menu,
        _format_command_detail,
        _format_menu_entries,
        _format_plugin_menu,
    )

    fixture     = load_production_catalog()
    pendo_roots = _find_plugin_roots(fixture.roots, "pendo")
    plugin_page = _format_plugin_menu(pendo_roots, page=1)
    todo = _find_exact_catalog_nodes(fixture.roots, "pendo todo")[0]
    todo_page = _format_branch_menu(todo, page=1)
    todo_add = _find_exact_catalog_nodes(fixture.roots, "pendo todo add")[0]
    detail   = _format_command_detail(todo_add)

    assert "📦 pendo  1/3" in plugin_page
    assert "/pendo event" in plugin_page
    assert "/pendo event add" not in plugin_page
    assert "命令码：" not in plugin_page
    assert "正确示例" not in plugin_page

    assert "📂 /pendo todo  1/1" in todo_page
    assert "/pendo todo add" in todo_page
    assert "/pendo todo add [内容]" not in todo_page
    assert "继续查看：/help pendo todo add" in todo_page

    assert detail.startswith("📘 命令详情")
    assert "/pendo todo add [内容]" in detail
    assert "正确示例" in detail
    assert "错误示例" in detail
    assert "命令码：pendo.pendo.todo.add" in detail

    for node in (item for root in fixture.roots for item in root.walk()):
        lines = _format_menu_entries((node,))
        assert max(map(_display_width, lines)) <= HELP_MOBILE_LINE_WIDTH, node.code


class _ConfigProvider:
    config: ClassVar[dict[str, Any]] = {
        "bot_name"                 : "TestBot",
        "command_prefixes"         : ["/"],
        "require_bot_name_in_group": False,
    }


class _PluginRegistry:
    def get(self, _name: str) -> None:
        return None


class _AdminCheck:
    def __init__(self, *, is_admin: bool = True) -> None:
        self._is_admin = is_admin

    def is_admin(self, _user_id: int | None) -> bool:
        return self._is_admin

    def issue_user_principal(
        self,
        _event: dict[str, Any],
        *,
        user_id: int | None,
        group_id: int | None,
        is_private: bool,
    ) -> PluginPrincipal:
        assert user_id is not None
        sender = _event.get("sender")
        role   = sender.get("role", "unknown") if isinstance(sender, dict) else "unknown"
        return PluginPrincipal(
            kind         = "user",
            user_id      = user_id,
            group_id     = group_id,
            is_bot_admin = self._is_admin,
            is_private   = is_private,
            group_role   = "unknown" if is_private else str(role),
        )


_EVENT_HEADERS = {
    "Authorization"           : "Bearer catalog-test-token",
    "X-XiaoQing-Response-Mode": "actions",
}


async def _start_event_server(
    fixture: CatalogFixture,
    *,
    port: int,
    is_admin: bool = True,
) -> StandaloneInboundServer:
    """用生产分发器在空闲端口上真实启动入站服务器。"""

    def build_context(
        _plugin_name: str,
        _user_id: int | None,
        _group_id: int | None,
        _request_id: str,
        principal: PluginPrincipal,
    ) -> Any:
        return SimpleNamespace(principal=principal, command_invocation=None)

    dispatcher = Dispatcher(
        router          = fixture.router,
        config_provider = _ConfigProvider(),
        plugin_registry = _PluginRegistry(),
        admin_check=_AdminCheck(is_admin=is_admin),
        build_context = build_context,
        semaphore     = AdjustableSemaphore(10),
    )
    server = StandaloneInboundServer(
        host           = "127.0.0.1",
        port           = port,
        token          = "catalog-test-token",
        handler        = dispatcher.handle_event,
        enable_http    = True,
        enable_ws      = False,
        ws_max_workers = 1,
        ws_queue_size  = 1,
    )
    await server.start()
    return server


async def _post_event(
    session: ClientSession,
    port: int,
    payload: dict[str, Any],
) -> tuple[int, str]:
    async with session.post(
        f"http://127.0.0.1:{port}/event",
        json    = payload,
        headers = _EVENT_HEADERS,
    ) as response:
        return response.status, await response.text()


def _onebot_payload(
    text: str,
    contexts: tuple[str, ...],
    *,
    scope: str | None = None,
    group_role: str   = "owner",
) -> dict[str, Any]:
    """按最终目录节点允许的场景构造真实 OneBot 消息事件。"""

    group_mode = (
        scope == "group"
        if scope is not None
        else ("private" not in contexts and "group" in contexts)
    )
    payload: dict[str, Any] = {
        "post_type"   : "message",
        "message_type": "group" if group_mode else "private",
        "user_id"     : 10001,
        "self_id"     : 90001,
        "raw_message" : text,
        "message"     : [{"type": "text", "data": {"text": text}}],
    }
    if group_mode:
        payload["group_id"] = 20001
        payload["sender"]   = {"user_id": 10001, "role": group_role}
    return payload


@pytest.mark.asyncio
@pytest.mark.integration
async def test_every_normal_and_invalid_example_passes_through_event(
    unused_tcp_port: int,
) -> None:
    """所有目录样例必须经过生产 ``/event`` 校验、归一化和 Core 分发链。"""

    fixture = load_production_catalog()
    server = await _start_event_server(fixture, port=unused_tcp_port)

    case_count = 0
    try:
        async with ClientSession() as session:
            for root in fixture.roots:
                for declared_node in root.walk():
                    cases = (
                        *(("normal", example) for example in declared_node.examples),
                        *(("invalid", example) for example in declared_node.invalid_examples),
                    )
                    for kind, example in cases:
                        _spec, expected = _resolve_example(fixture.router, example)
                        before = len(fixture.invocations)
                        status, text = await _post_event(
                            session,
                            unused_tcp_port,
                            _onebot_payload(example, expected.node.contexts),
                        )

                        assert status == 200, (
                            f"/event 拒绝 {declared_node.code} 的 {kind} 样例: {example}; "
                            f"body={text}"
                        )
                        assert len(fixture.invocations) == before + 1, (
                            f"{declared_node.code} 的 {kind} 样例没有进入插件命令处理器: {example}"
                        )
                        actual = fixture.invocations[-1]
                        assert actual.root.code == root.code
                        assert actual.node.code in text
                        if kind == "normal":
                            assert declared_node.code in {node.code for node in actual.chain}
                        case_count += 1
    finally:
        await server.stop()

    assert case_count >= sum(len(root.walk()) * 2 for root in fixture.roots)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_catalog_permissions_and_contexts_fail_closed_before_handlers(
    unused_tcp_port_factory,
) -> None:
    """目录声明的权限和场景必须由 Core 统一拦截，不能依赖各插件自觉检查。"""

    fixture    = load_production_catalog()
    admin_port = unused_tcp_port_factory()
    user_port  = unused_tcp_port_factory()
    admin_server = await _start_event_server(fixture, port=admin_port, is_admin=True)
    user_server = None
    try:
        user_server = await _start_event_server(fixture, port=user_port, is_admin=False)
        async with ClientSession() as session:
            for root in fixture.roots:
                for node in root.walk():
                    example = node.examples[0]
                    if node.permission != "public":
                        before = len(fixture.invocations)
                        scope  = "private" if "private" in node.contexts else "group"
                        status, text = await _post_event(
                            session,
                            user_port,
                            _onebot_payload(
                                example,
                                node.contexts,
                                scope      = scope,
                                group_role = "member",
                            ),
                        )
                        body = json.dumps(json.loads(text), ensure_ascii=False)
                        expected = "管理员" if node.permission == "group_admin" else "权限不足"
                        assert status == 200
                        assert expected in body, f"{node.code} 未按目录权限拒绝: {body}"
                        assert len(fixture.invocations) == before

                    for denied_scope in {"private", "group"} - set(node.contexts):
                        before = len(fixture.invocations)
                        status, text = await _post_event(
                            session,
                            admin_port,
                            _onebot_payload(example, node.contexts, scope=denied_scope),
                        )
                        body = json.dumps(json.loads(text), ensure_ascii=False)
                        assert status == 200
                        assert "当前会话类型不支持此命令" in body, (
                            f"{node.code} 未按目录场景拒绝: {body}"
                        )
                        assert len(fixture.invocations) == before
    finally:
        await admin_server.stop()
        if user_server is not None:
            await user_server.stop()


# ──────────────────── 读写性质声明 ────────────────────


def _manifest_with(**node: Any) -> dict[str, Any]:
    return {
        "name"    : "demo",
        "commands": [
            {
                "name"       : "demo",
                "triggers"   : ["demo"],
                "help"       : "示例",
                "subcommands": [
                    {"name": "record", "help": "记一条", "usage": "/demo record", **node}
                ],
            }
        ],
    }


def test_effect_reaches_the_published_catalog():
    manifest = PluginManifest.model_validate(_manifest_with(effect="write"))
    root = build_command_catalog_node("demo", manifest.commands[0].model_dump(), root=True)
    record = root.children[0]
    assert (root.effect, record.effect) == (None, "write")
    assert record.to_dict()["effect"] == "write"
    json.dumps(root.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "node",
    [
        {"effect": "delete"},
        # 命令声明里没有定义的字段（例如专用工具的结构化参数）会被拒绝。
        {"structured_input": {"name": "demo_record", "parameters": {"type": "object"}}},
    ],
)
def test_invalid_command_declarations_are_rejected(node):
    with pytest.raises(ValueError):
        PluginManifest.model_validate(_manifest_with(**node))
