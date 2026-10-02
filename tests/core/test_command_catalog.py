"""全插件命令目录契约，以及统一 ``/event`` 入站路由门禁。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from core.dispatcher import Dispatcher
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
from core.server import InboundServer
from tests.helpers.paths import REPOSITORY_ROOT

PROJECT_ROOT     = REPOSITORY_ROOT
PLUGIN_MANIFESTS = tuple(sorted((PROJECT_ROOT / "plugins").glob("*/plugin.json")))


@dataclass(frozen=True, slots=True)
class _CatalogFixture:
    manifests: tuple[PluginManifest, ...]
    roots: tuple[CommandCatalogNode, ...]
    router: CommandRouter
    invocations: list[CommandInvocation]


def _load_catalog() -> _CatalogFixture:
    """从生产 manifest 构建与 PluginManager 相同的不可变目录。"""

    manifests = tuple(
        PluginManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in PLUGIN_MANIFESTS
    )
    roots: list[CommandCatalogNode]      = []
    router                               = CommandRouter()
    invocations: list[CommandInvocation] = []

    async def catalog_handler(
        _command: str,
        _args: str,
        _event: dict[str, Any],
        context: Any,
    ) -> list[dict[str, Any]]:
        invocation = context.command_invocation
        invocations.append(invocation)
        return [{"type": "text", "data": {"text": invocation.node.code}}]

    for manifest in manifests:
        for command in manifest.commands:
            root = build_command_catalog_node(
                manifest.name,
                command.model_dump(),
                root=True,
            )
            roots.append(root)
            router.register(
                CommandSpec(
                    plugin     = manifest.name,
                    name       = command.name,
                    triggers   = command.triggers,
                    help_text  = command.help,
                    admin_only = command.admin_only,
                    handler    = catalog_handler,
                    priority   = command.priority,
                    usage      = command.usage,
                    catalog    = root,
                )
            )
    return _CatalogFixture(manifests, tuple(roots), router, invocations)


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

    fixture = _load_catalog()
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


def test_sensitive_command_surfaces_declare_private_contexts() -> None:
    """高权限与个人数据入口必须由发布目录声明私聊边界。"""

    fixture              = _load_catalog()
    private_root_plugins = {
        "shell",
        "jupyter",
        "qingssh",
        "codex",
        "minecraft",
        "pendo",
    }
    for root in fixture.roots:
        if root.plugin in private_root_plugins:
            assert root.contexts == ("private",), root.code

    nodes                  = {node.code: node for root in fixture.roots for node in root.walk()}
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

    fixture = _load_catalog()
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

    fixture     = _load_catalog()
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
        "bot_name": "TestBot",
        "command_prefixes": ["/"],
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


class _Request:
    """足够调用生产 ``InboundServer.post_event`` 的 HTTP 请求替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.headers = {
            "Authorization": "Bearer catalog-test-token",
            "Content-Type": "application/json",
            "X-XiaoQing-Response-Mode": "actions",
        }
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload


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
        "post_type": "message",
        "message_type": "group" if group_mode else "private",
        "user_id": 10001,
        "self_id": 90001,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    if group_mode:
        payload["group_id"] = 20001
        payload["sender"]   = {"user_id": 10001, "role": group_role}
    return payload


@pytest.mark.asyncio
@pytest.mark.integration
async def test_every_normal_and_invalid_example_passes_through_event() -> None:
    """所有目录样例必须经过生产 ``/event`` 校验、归一化和 Core 分发链。"""

    fixture = _load_catalog()

    def build_context(
        _plugin_name: str,
        _user_id: int | None,
        _group_id: int | None,
        _request_id: str,
        principal: PluginPrincipal,
    ) -> Any:
        context = SimpleNamespace(principal=principal, command_invocation=None)
        return context

    dispatcher = Dispatcher(
        router          = fixture.router,
        config_provider = _ConfigProvider(),
        plugin_registry = _PluginRegistry(),
        admin_check     = _AdminCheck(),
        build_context   = build_context,
        semaphore       = None,
    )
    server = InboundServer(
        host           = "127.0.0.1",
        port           = 8765,
        token          = "catalog-test-token",
        handler        = dispatcher.handle_event,
        enable_http    = True,
        enable_ws      = False,
        ws_max_workers = 1,
        ws_queue_size  = 0,
    )

    case_count = 0
    try:
        for root in fixture.roots:
            for declared_node in root.walk():
                cases = (
                    *(("normal", example) for example in declared_node.examples),
                    *(("invalid", example) for example in declared_node.invalid_examples),
                )
                for kind, example in cases:
                    _spec, expected = _resolve_example(fixture.router, example)
                    before   = len(fixture.invocations)
                    response = await server.post_event(
                        _Request(_onebot_payload(example, expected.node.contexts))
                    )

                    assert response.status == 200, (
                        f"/event 拒绝 {declared_node.code} 的 {kind} 样例: {example}; "
                        f"body={response.text}"
                    )
                    assert len(fixture.invocations) == before + 1, (
                        f"{declared_node.code} 的 {kind} 样例没有进入插件命令处理器: {example}"
                    )
                    actual = fixture.invocations[-1]
                    assert actual.root.code == root.code
                    assert actual.node.code in response.text
                    if kind == "normal":
                        assert declared_node.code in {node.code for node in actual.chain}
                    case_count += 1
    finally:
        await server._event_dispatcher.stop()

    assert case_count >= sum(len(root.walk()) * 2 for root in fixture.roots)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_catalog_permissions_and_contexts_fail_closed_before_handlers() -> None:
    """目录声明的权限和场景必须由 Core 统一拦截，不能依赖各插件自觉检查。"""

    fixture = _load_catalog()

    def build_context(
        _plugin_name: str,
        _user_id: int | None,
        _group_id: int | None,
        _request_id: str,
        principal: PluginPrincipal,
    ) -> Any:
        return SimpleNamespace(principal=principal, command_invocation=None)

    def make_server(*, is_admin: bool) -> InboundServer:
        dispatcher = Dispatcher(
            router          = fixture.router,
            config_provider = _ConfigProvider(),
            plugin_registry = _PluginRegistry(),
            admin_check=_AdminCheck(is_admin=is_admin),
            build_context = build_context,
            semaphore     = None,
        )
        return InboundServer(
            host           = "127.0.0.1",
            port           = 8765,
            token          = "catalog-test-token",
            handler        = dispatcher.handle_event,
            enable_http    = True,
            enable_ws      = False,
            ws_max_workers = 1,
            ws_queue_size  = 0,
        )

    admin_server = make_server(is_admin=True)
    user_server = make_server(is_admin=False)
    try:
        for root in fixture.roots:
            for node in root.walk():
                example = node.examples[0]
                if node.permission != "public":
                    before   = len(fixture.invocations)
                    scope    = "private" if "private" in node.contexts else "group"
                    response = await user_server.post_event(
                        _Request(
                            _onebot_payload(
                                example,
                                node.contexts,
                                scope      = scope,
                                group_role = "member",
                            )
                        )
                    )
                    body = json.dumps(json.loads(response.text), ensure_ascii=False)
                    expected = "管理员" if node.permission == "group_admin" else "权限不足"
                    assert response.status == 200
                    assert expected in body, f"{node.code} 未按目录权限拒绝: {body}"
                    assert len(fixture.invocations) == before

                for denied_scope in {"private", "group"} - set(node.contexts):
                    before   = len(fixture.invocations)
                    response = await admin_server.post_event(
                        _Request(
                            _onebot_payload(
                                example,
                                node.contexts,
                                scope=denied_scope,
                            )
                        )
                    )
                    body = json.dumps(json.loads(response.text), ensure_ascii=False)
                    assert response.status == 200
                    assert "当前会话类型不支持此命令" in body, (
                        f"{node.code} 未按目录场景拒绝: {body}"
                    )
                    assert len(fixture.invocations) == before
    finally:
        await admin_server._event_dispatcher.stop()
        await user_server._event_dispatcher.stop()
