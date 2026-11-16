"""Pendo Web 命令、搜索导入和数据展示边界。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.fixture
def web_auth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """让认证模块从未绑定数据库和密钥的状态开始，结束时复位（模块级状态隔离）。"""

    auth_module = pytest.importorskip("plugins.pendo.web.auth")
    monkeypatch.delenv("PENDO_WEB_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(auth_module, "_AUTH_DATABASE", None)
    monkeypatch.setattr(auth_module, "_SECRET_FILE", None)
    monkeypatch.setattr(auth_module, "_SECRET_CACHE", None)


class TestPendoWebHandler:
    """测试 pendo web 命令格式化与发送行为"""

    def test_web_token_sends_raw_code_as_separate_private_message(self, db, web_auth_state):
        from plugins.pendo.handlers import web as web_module
        from plugins.pendo.web.auth import consume_login_code

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=db)

        result = asyncio.run(handler.handle("1001", "token", context=context))

        assert result["status"] == "success"
        assert "登录 Code 已单独私聊发送" in result["message"]
        assert "Code 7 天内可兑换一次" in result["message"]
        assert "浏览器会话保持 7 天" in result["message"]
        assert len(actions) == 1
        assert actions[0]["action"] == "send_private_msg"
        assert actions[0]["params"]["user_id"] == 1001
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert token_text not in result["message"]
        assert "http://" not in token_text
        assert "https://" not in token_text
        lifetime = (
            db.get_connection()
            .execute("SELECT expires_at - issued_at FROM login_code_registry")
            .fetchone()[0]
        )
        assert lifetime == 7 * 24 * 60 * 60
        assert consume_login_code(token_text, db=db) == "1001"

    def test_web_token_fails_closed_when_private_delivery_is_unavailable(
        self, db, web_auth_state, monkeypatch
    ):
        import secrets

        from plugins.pendo.handlers import web as web_module

        monkeypatch.setattr(secrets, "token_urlsafe", lambda _size: "mock-code")

        handler = web_module.WebHandler(db=db)
        result = asyncio.run(handler.handle("1001", "token", context=None))

        assert result["status"] == "error"
        assert "无法通过私聊安全发送凭据" in result["message"]
        assert "mock-code" not in result["message"]
        assert "登录 Code:" not in result["message"]

    def test_web_token_reports_unknown_private_delivery_without_exposing_credential(self):
        from plugins.pendo.handlers.web import WebHandler

        async def unknown_delivery(_action):
            return None

        outcome = asyncio.run(
            WebHandler._send_private_text(
                SimpleNamespace(send_action=unknown_delivery),
                "1001",
                "secret-token",
            )
        )
        result = WebHandler._build_token_result(
            token_sent        = outcome,
            header            = "Pendo Web",
            success_line      = "generated",
            expiry_text       = "5 minutes",
            private_hint      = "sent",
            private_copy_hint = "copy",
        )

        assert outcome is None
        assert result["status"] == "success"
        assert "未收到最终投递回执" in result["message"]
        assert "secret-token" not in result["message"]

    def test_web_start_surfaces_last_start_error(self, monkeypatch, unused_tcp_port_factory):
        from plugins.pendo.config import PendoConfig
        from plugins.pendo.handlers import web as web_module
        from plugins.pendo.web import server as server_module

        # 真实的服务模块拒绝把 Web 绑到非 loopback 地址而不启用 Secure Cookie：
        # 启动在绑定端口之前就失败，不会真的监听。
        monkeypatch.setattr(server_module, "_last_error", None)
        PendoConfig.configure({"web_host": "0.0.0.0", "web_port": unused_tcp_port_factory()})

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(handler.handle("1001", "start", context=SimpleNamespace()))

        assert result["status"] == "error"
        assert "服务启动失败" in result["message"]
        assert "非 loopback 地址" in result["message"]
        assert "plugins.pendo.web_port" in result["message"]
        assert not server_module.is_managed_running()

    def test_web_stop_reports_external_running_server_without_failing(
        self, unused_tcp_port_factory
    ):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from plugins.pendo.config import PendoConfig
        from plugins.pendo.handlers import web as web_module

        class _ExternalPage(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                return

        # 同一个端口上另有一个不是本插件线程启动的 HTTP 服务。
        port = unused_tcp_port_factory()
        PendoConfig.configure({"web_port": port})
        external = HTTPServer(("127.0.0.1", port), _ExternalPage)
        thread = threading.Thread(target=external.serve_forever, daemon=True)
        thread.start()
        try:
            handler = web_module.WebHandler(db=None)
            result = asyncio.run(handler.handle("1001", "stop", context=SimpleNamespace()))
        finally:
            external.shutdown()
            external.server_close()
            thread.join(timeout=5)

        assert result["status"] == "success"
        assert "外部服务" in result["message"]

    def test_web_widget_token_sends_token_as_separate_private_message(self, db, web_auth_state):
        from plugins.pendo.handlers import web as web_module
        from plugins.pendo.web.auth import verify_token

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=db)

        result = asyncio.run(handler.handle("1001", "widget-token", context=context))

        assert result["status"] == "success"
        assert "Widget Token 已单独私聊发送" in result["message"]
        assert "365 天" in result["message"]
        assert len(actions) == 1
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert "Pendo Web Widget Token" in token_text
        token = token_text.splitlines()[1]
        assert token not in result["message"]
        payload = verify_token(token, db=db)
        assert payload["owner_id"] == "1001"
        assert payload["exp"] - payload["iat"] == 365 * 24 * 60 * 60

    def test_web_widget_revoke_revokes_only_callers_registered_tokens(self, db, web_auth_state):
        from plugins.pendo.handlers import web as web_module
        from plugins.pendo.web.auth import AuthError, generate_widget_token, verify_token

        caller_tokens = [generate_widget_token("1001", db=db) for _ in range(2)]
        other_token = generate_widget_token("1002", db=db)
        handler = web_module.WebHandler(db=db)

        result = asyncio.run(handler.handle("1001", "widget-revoke", context=None))

        assert result == {
            "status" : "success",
            "message": "✅ 已吊销 2 个 Widget Token；请重新生成并录入 Keychain",
        }
        for token in caller_tokens:
            with pytest.raises(AuthError, match="revoked"):
                verify_token(token, db=db)
        assert verify_token(other_token, db=db)["owner_id"] == "1002"


class TestSearchAndImport:
    def test_search_handler_applies_date_field_for_range_filters(self):
        from plugins.pendo.handlers.search import SearchHandler

        calls = []

        class _ItemsRepo:
            def search_items_page(self, owner_id, query, filters, *, limit, offset, **_options):
                calls.append((owner_id, query, filters))
                assert limit == 15
                assert offset == 0
                return [], 0

        handler = SearchHandler(_ItemsRepo())
        result  = asyncio.run(
            handler.search("u1", "会议 type=event range=last7d", context=SimpleNamespace())
        )

        assert result["status"] == "success"
        assert calls
        assert calls[0][2]["date_field"] == "start_time"
        assert "start_date" in calls[0][2]
        assert "end_date" in calls[0][2]

    def test_batch_insert_or_update_refreshes_fts_rows(self, tmp_path):

        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))
        try:
            db.batch_insert_or_update(
                [
                    (
                        "insert",
                        {
                            "id"      : "note-1",
                            "type"    : "note",
                            "title"   : "脉冲星速记",
                            "content" : "第一次导入内容",
                            "category": "研究",
                            "tags"    : ["memo"],
                        },
                    )
                ],
                "u1",
            )
            conn      = db.get_connection()
            first_row = conn.execute(
                "SELECT title, content FROM items_fts WHERE id = ?",
                ("note-1",),
            ).fetchone()
            assert first_row is not None
            assert first_row["title"] == "脉冲星速记"

            db.batch_insert_or_update(
                [
                    (
                        "update",
                        {
                            "id"      : "note-1",
                            "type"    : "note",
                            "title"   : "脉冲星速记",
                            "content" : "更新后的导入内容",
                            "category": "研究",
                            "tags"    : ["memo"],
                        },
                    )
                ],
                "u1",
            )
            updated_row = conn.execute(
                "SELECT content FROM items_fts WHERE id = ?",
                ("note-1",),
            ).fetchone()
            assert updated_row is not None
            assert updated_row["content"] == "更新后的导入内容"
        finally:
            db.cleanup()


class TestAmountsRangesAndCategories:
    def test_widget_ledger_panel_prefers_amount_cents(self, tmp_path):

        from plugins.pendo.services.db import Database
        from plugins.pendo.web.api.widget import build_widget_summary

        db       = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-widget-ledger"
        try:
            db.insert_item(
                {
                    "id"              : "widget-ledger-expense",
                    "owner_id"        : owner_id,
                    "type"            : "ledger",
                    "title"           : "TEST_SCRIPTABLE 午饭",
                    "amount"          : 0,
                    "amount_cents"    : 12345,
                    "transaction_type": "expense",
                    "ledger_category" : "餐饮",
                    "ledger_date"     : "2026-04-30",
                    "account_name"    : "微信",
                }
            )

            summary = build_widget_summary(
                db,
                owner_id,
                section = "ledger",
                now     = "2026-04-30T12:00:00",
            )

            assert summary["panel"]["items"][0]["amount_text"] == "-¥123"
            assert summary["panel"]["summary"]["primary"] == "支出 ¥123"
        finally:
            db.cleanup()

    def test_export_range_includes_event_spanning_into_window(self):
        from datetime import date

        from plugins.pendo.web.api.transfer import item_matches_range

        spanning_event = SimpleNamespace(
            start_time = "2026-04-29T23:00:00+08:00",
            end_time   = "2026-04-30T01:00:00+08:00",
        )
        before_event = SimpleNamespace(
            start_time = "2026-04-29T20:00:00+08:00",
            end_time   = "2026-04-29T21:00:00+08:00",
        )

        assert item_matches_range(spanning_event, "event", date(2026, 4, 30), date(2026, 4, 30))
        assert not item_matches_range(before_event, "event", date(2026, 4, 30), date(2026, 4, 30))

    def test_collection_category_search_uses_collection_fallback(self, tmp_path):

        from plugins.pendo.services.db import Database

        db       = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-search-collection"
        try:
            collection_id = db.create_event_collection(
                {
                    "id"      : "test_collection_category",
                    "owner_id": owner_id,
                    "kind"    : "multi_node",
                    "title"   : "TEST_STATS 学术会议",
                    "category": "工作",
                    "location": "上海",
                }
            )
            db.insert_item(
                {
                    "id"                   : "test_collection_node",
                    "owner_id"             : owner_id,
                    "type"                 : "event",
                    "title"                : "摘要截止",
                    "category"             : "",
                    "start_time"           : "2026-05-10T09:00:00",
                    "event_role"           : "multi_node_child",
                    "event_collection_id"  : collection_id,
                    "event_collection_kind": "multi_node",
                    "event_index"          : 1,
                }
            )

            rows, _total = db.search_items_page(
                owner_id,
                "学术会议",
                {"type": "event", "category": "工作"},
            )

            assert [row.id for row in rows] == ["test_collection_node"]
        finally:
            db.cleanup()


def test_web_lifecycle_is_declared_for_bot_admins():
    """启动、停止共享 Web 服务只给 Bot 管理员：写在 plugin.json 里，由 Core 统一检查。"""

    import json

    from core.router import build_command_catalog_node
    from tests.helpers.paths import REPOSITORY_ROOT

    manifest = json.loads(
        (REPOSITORY_ROOT / "plugins" / "pendo" / "plugin.json").read_text(encoding="utf-8")
    )
    root = build_command_catalog_node("pendo", manifest["commands"][0], root=True)
    web = root.resolve_child("web")
    assert web is not None and web.permission == "public"
    for action in ("start", "stop"):
        node = web.resolve_child(action)
        assert node is not None and node.permission == "bot_admin", action
