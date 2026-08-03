"""全插件命令目录契约，以及统一 ``/event`` 入站路由门禁。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from aiohttp import WSMsgType

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
from scripts.run_command_matrix import (
    EventClient,
    EventResponse,
    MatrixCase,
    WebSocketEventClient,
    _probe_health,
    build_matrix,
    evaluate_response,
    fetch_runtime_catalog,
    load_policy,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MANIFESTS = tuple(sorted((PROJECT_ROOT / "plugins").glob("*/plugin.json")))
MATRIX_POLICY = PROJECT_ROOT / "tests" / "command_matrix_policy.json"


def test_event_clients_reserve_disjoint_message_id_ranges() -> None:
    common = {
        "endpoint": "http://127.0.0.1:12000/event",
        "token": "test-token",
        "admin_id": 1,
        "user_id": 2,
        "group_id": 3,
        "timeout": 1.0,
    }

    first = EventClient(**common)
    second = EventClient(**common)

    assert second._message_seed - first._message_seed >= 1_000_000


def test_websocket_event_client_uses_real_bearer_transport_and_collects_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class WebSocket:
        def __init__(self) -> None:
            self.receive_count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send_json(self, event: dict[str, Any]) -> None:
            captured["event"] = event

        async def receive(self, *, timeout: float):
            captured.setdefault("receive_timeouts", []).append(timeout)
            self.receive_count += 1
            if self.receive_count == 1:
                return SimpleNamespace(
                    type=WSMsgType.TEXT,
                    data=json.dumps(
                        {
                            "action": "send_private_msg",
                            "params": {"message": "pong"},
                        }
                    ),
                )
            raise TimeoutError

    websocket = WebSocket()

    class Session:
        def __init__(self, *, timeout: Any) -> None:
            captured["client_timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def ws_connect(self, endpoint: str, *, headers: dict[str, str], timeout: float):
            captured["endpoint"] = endpoint
            captured["headers"] = headers
            captured["connect_timeout"] = timeout
            return websocket

    monkeypatch.setattr("scripts.run_command_matrix.aiohttp.ClientSession", Session)
    client = WebSocketEventClient(
        endpoint="http://127.0.0.1:12000/ws",
        token="runtime-token",
        admin_id=1,
        user_id=2,
        group_id=3,
        timeout=4.5,
    )

    response = client.send("/echo pong", scope="group", actor="group_owner")

    assert response.status == 200
    assert response.transport_error is None
    assert response.payload == {
        "actions": [{"action": "send_private_msg", "params": {"message": "pong"}}]
    }
    assert captured["endpoint"] == "http://127.0.0.1:12000/ws"
    assert captured["headers"] == {"Authorization": "Bearer runtime-token"}
    assert captured["connect_timeout"] == 4.5
    assert captured["event"]["message"] == "/echo pong"
    assert captured["event"]["group_id"] == 3
    assert captured["event"]["sender"]["role"] == "owner"
    assert captured["receive_timeouts"][0] == 4.5


def test_command_matrix_health_probe_uses_runtime_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read() -> bytes:
            return b'{"status":"ok"}'

    def urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("scripts.run_command_matrix.urllib.request.urlopen", urlopen)

    result = _probe_health(
        "http://127.0.0.1:12000/event",
        "runtime-token",
        timeout=3.5,
    )

    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:12000/health"
    assert request.get_header("Authorization") == "Bearer runtime-token"
    assert captured["timeout"] == 3.5
    assert result["status"] == 200
    assert result["payload"] == {"status": "ok"}


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
    roots: list[CommandCatalogNode] = []
    router = CommandRouter()
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
                    plugin=manifest.name,
                    name=command.name,
                    triggers=command.triggers,
                    help_text=command.help,
                    admin_only=command.admin_only,
                    handler=catalog_handler,
                    priority=command.priority,
                    usage=command.usage,
                    catalog=root,
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


def _runtime_catalog_records(roots: tuple[CommandCatalogNode, ...]) -> list[dict[str, Any]]:
    """复现 ``/help json`` 的扁平运行态目录格式。"""

    records: list[dict[str, Any]] = []
    for root in roots:
        for node in root.walk():
            record = node.to_dict()
            record["subcommands"] = [child.code for child in node.children]
            records.append(record)
    return records


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

    fixture = _load_catalog()
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

    nodes = {node.code: node for root in fixture.roots for node in root.walk()}
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


def test_live_matrix_is_generated_from_every_catalog_node_and_alias() -> None:
    """运行态 Runner 必须自动扩展正反例、场景、别名和权限拒绝用例。"""

    fixture = _load_catalog()
    policy = load_policy(MATRIX_POLICY)
    assert set(policy["plugins"]) == {manifest.name for manifest in fixture.manifests}

    records = _runtime_catalog_records(fixture.roots)
    cases = build_matrix(records, policy)
    nodes = tuple(node for root in fixture.roots for node in root.walk())
    codes = {node.code for node in nodes}

    assert {case.code for case in cases if case.kind == "normal"} == codes
    assert {case.code for case in cases if case.kind == "invalid"} == codes
    assert len({case.case_id for case in cases}) == len(cases)
    assert sum(case.kind == "normal" for case in cases) == sum(
        len(node.examples) * len(node.contexts) for node in nodes
    )
    assert sum(case.kind == "invalid" for case in cases) == sum(
        len(node.invalid_examples) * len(node.contexts) for node in nodes
    )
    assert sum(case.kind == "alias" for case in cases) == sum(
        len(node.aliases) * len(node.contexts) for node in nodes
    )
    assert sum(case.kind == "permission_denied" for case in cases) == sum(
        len(node.contexts) for node in nodes if node.permission != "public"
    )
    assert sum(case.kind == "context_denied" for case in cases) == sum(
        len({"private", "group"} - set(node.contexts)) for node in nodes
    )

    # 别名用例不仅进入计划，还必须真的解析到声明该别名的目录节点。
    for case in (item for item in cases if item.kind == "alias"):
        _spec, invocation = _resolve_example(fixture.router, case.message)
        assert case.code in {node.code for node in invocation.chain}, (
            f"{case.code} 的别名用例路由错误: {case.message}"
        )


def test_runtime_catalog_is_read_from_every_help_json_page() -> None:
    """运行态抓取必须校验分页元数据并合并全部命令码。"""

    pages = {
        1: {
            "query": None,
            "page": 1,
            "total_pages": 2,
            "commands": [{"code": "alpha.one", "plugin": "alpha"}],
        },
        2: {
            "query": None,
            "page": 2,
            "total_pages": 2,
            "commands": [{"code": "beta.two", "plugin": "beta"}],
        },
    }

    class PagedClient:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send(self, message: str, *, scope: str, actor: str) -> EventResponse:
            self.messages.append(message)
            page = int(message.rsplit(" ", 1)[1])
            text = json.dumps(pages[page], ensure_ascii=False)
            payload = {
                "actions": [{"params": {"message": [{"type": "text", "data": {"text": text}}]}}]
            }
            assert scope == "private"
            assert actor == "bot_admin"
            return EventResponse(200, payload, 1.0)

    client = PagedClient()
    records = fetch_runtime_catalog(client)
    assert [record["code"] for record in records] == ["alpha.one", "beta.two"]
    assert client.messages == ["/help json page 1", "/help json page 2"]


def test_runtime_result_keeps_contract_and_semantic_strength_separate() -> None:
    """有回复不等于业务语义通过；Core 固定拒绝才计严格断言。"""

    base = {
        "case_id": "case-1",
        "plugin": "demo",
        "code": "demo.command",
        "example_index": 1,
        "message": "/demo",
        "scope": "private",
        "actor": "user",
        "permission": "public",
        "risk": "read_only",
        "dependency": "local",
        "sensitive": False,
        "policy_reason": "test",
    }
    reply = EventResponse(
        200,
        {"actions": [{"params": {"message": [{"type": "text", "data": {"text": "业务已处理"}}]}}]},
        2.0,
    )
    normal = MatrixCase(
        **base,
        kind="normal",
        semantic_expectation="observable_business_reply",
    )
    normal_result = evaluate_response(normal, reply, redactions=())
    assert normal_result["execution_status"] == "passed_runtime_contract"
    assert normal_result["semantic_strength"] == "observable_reply"

    strict_invalid = MatrixCase(
        **base,
        kind="invalid",
        semantic_expectation="rejected_without_unhandled_exception",
        invalid_expect_any=("用法", "参数错误"),
    )
    rejected = evaluate_response(
        strict_invalid,
        EventResponse(
            200,
            {
                "actions": [
                    {
                        "params": {
                            "message": [{"type": "text", "data": {"text": "参数错误，请查看用法"}}]
                        }
                    }
                ]
            },
            1.0,
        ),
        redactions=(),
    )
    assert rejected["execution_status"] == "passed_runtime_contract"
    assert rejected["semantic_strength"] == "strict_invalid_assertion"

    misrouted = evaluate_response(strict_invalid, reply, redactions=())
    assert misrouted["execution_status"] == "failed"
    assert misrouted["failure"] == "invalid_rejection_not_observed"

    denied = MatrixCase(
        **{
            **base,
            "case_id": "case-2",
            "permission": "bot_admin",
            "kind": "permission_denied",
            "semantic_expectation": "permission_denied_by_core",
        }
    )
    denial_reply = EventResponse(
        200,
        {"actions": [{"params": {"message": [{"type": "text", "data": {"text": "权限不足"}}]}}]},
        1.0,
    )
    denied_result = evaluate_response(denied, denial_reply, redactions=())
    assert denied_result["execution_status"] == "passed_runtime_contract"
    assert denied_result["semantic_strength"] == "strict_core_assertion"

    internal_error = EventResponse(
        200,
        {
            "actions": [
                {
                    "params": {
                        "message": [
                            {
                                "type": "text",
                                "data": {"text": "XQ-PLUGIN-UNEXPECTED"},
                            }
                        ]
                    }
                }
            ]
        },
        1.0,
    )
    failed = evaluate_response(normal, internal_error, redactions=())
    assert failed["execution_status"] == "failed"
    assert failed["failure"] == "unhandled_plugin_exception"


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
        role = sender.get("role", "unknown") if isinstance(sender, dict) else "unknown"
        return PluginPrincipal(
            kind="user",
            user_id=user_id,
            group_id=group_id,
            is_bot_admin=self._is_admin,
            is_private=is_private,
            group_role="unknown" if is_private else str(role),
        )


class _Request:
    """足够调用生产 ``InboundServer.post_event`` 的 HTTP 请求替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.headers = {
            "Authorization": "Bearer catalog-test-token",
            "Content-Type": "application/json",
        }
        self._payload = payload

    async def json(self) -> dict[str, Any]:
        return self._payload


def _onebot_payload(
    text: str,
    contexts: tuple[str, ...],
    *,
    scope: str | None = None,
    group_role: str = "owner",
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
        payload["sender"] = {"user_id": 10001, "role": group_role}
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
        router=fixture.router,
        config_provider=_ConfigProvider(),
        plugin_registry=_PluginRegistry(),
        admin_check=_AdminCheck(),
        build_context=build_context,
        semaphore=None,
    )
    server = InboundServer(
        host="127.0.0.1",
        port=8765,
        token="catalog-test-token",
        handler=dispatcher.handle_event,
        enable_http=True,
        enable_ws=False,
        ws_max_workers=1,
        ws_queue_size=0,
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
                    before = len(fixture.invocations)
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
            router=fixture.router,
            config_provider=_ConfigProvider(),
            plugin_registry=_PluginRegistry(),
            admin_check=_AdminCheck(is_admin=is_admin),
            build_context=build_context,
            semaphore=None,
        )
        return InboundServer(
            host="127.0.0.1",
            port=8765,
            token="catalog-test-token",
            handler=dispatcher.handle_event,
            enable_http=True,
            enable_ws=False,
            ws_max_workers=1,
            ws_queue_size=0,
        )

    admin_server = make_server(is_admin=True)
    user_server = make_server(is_admin=False)
    try:
        for root in fixture.roots:
            for node in root.walk():
                example = node.examples[0]
                if node.permission != "public":
                    before = len(fixture.invocations)
                    scope = "private" if "private" in node.contexts else "group"
                    response = await user_server.post_event(
                        _Request(
                            _onebot_payload(
                                example,
                                node.contexts,
                                scope=scope,
                                group_role="member",
                            )
                        )
                    )
                    body = json.dumps(json.loads(response.text), ensure_ascii=False)
                    expected = "管理员" if node.permission == "group_admin" else "权限不足"
                    assert response.status == 200
                    assert expected in body, f"{node.code} 未按目录权限拒绝: {body}"
                    assert len(fixture.invocations) == before

                for denied_scope in {"private", "group"} - set(node.contexts):
                    before = len(fixture.invocations)
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
