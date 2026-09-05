"""Pendo Web 命令、搜索导入和数据展示边界。"""

from __future__ import annotations

from tests.helpers.pendo_test_support import (
    ROOT,
    SimpleNamespace,
    asyncio,
    datetime,
)


class TestPendoWebHandler:
    """测试 pendo web 命令格式化与发送行为"""

    def test_web_token_sends_raw_code_as_separate_private_message(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url    = lambda: "http://127.0.0.1:8765",
                is_running = lambda: True,
                start      = lambda _db: True,
                stop       = lambda: True,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        issuance = {}

        def issue_code(owner_id, *, expires_seconds, db):
            issuance.update(owner_id=owner_id, expires_seconds=expires_seconds, db=db)
            return "mock-code"

        monkeypatch.setattr(web_module, "issue_login_code", issue_code)
        monkeypatch.setattr(web_module.web_server, "get_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(web_module.web_server, "is_running", lambda: True)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=None)

        result = asyncio.run(handler.handle("1001", "token", context=context))

        assert result["status"] == "success"
        assert "登录 Code 已单独私聊发送" in result["message"]
        assert "Code 7 天内可兑换一次" in result["message"]
        assert "浏览器会话保持 7 天" in result["message"]
        assert "mock-code" not in result["message"]
        assert issuance == {
            "owner_id": "1001",
            "expires_seconds": 7 * 24 * 60 * 60,
            "db": None,
        }
        assert len(actions) == 1
        assert actions[0]["action"] == "send_private_msg"
        assert actions[0]["params"]["user_id"] == 1001
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert token_text == "mock-code"
        assert "http://" not in token_text
        assert "https://" not in token_text

    def test_web_token_fails_closed_when_private_delivery_is_unavailable(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url    = lambda: "http://127.0.0.1:8765",
                is_running = lambda: False,
                start      = lambda _db: True,
                stop       = lambda: True,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        monkeypatch.setattr(web_module, "issue_login_code", lambda *_args, **_kwargs: "mock-code")
        monkeypatch.setattr(web_module.web_server, "get_url", lambda: "http://127.0.0.1:8765")
        monkeypatch.setattr(web_module.web_server, "is_running", lambda: False)

        handler = web_module.WebHandler(db=None)
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

    def test_web_start_surfaces_last_start_error(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url        = lambda: "http://127.0.0.1:8765",
                is_running     = lambda: False,
                start          = lambda _db: False,
                stop           = lambda: True,
                get_last_error = lambda: "无法绑定到 127.0.0.1:8765，端口可能已被占用。",
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(
            handler.handle(
                "1001", "start", context=types.SimpleNamespace(is_global_admin=lambda _uid: True)
            )
        )

        assert result["status"] == "error"
        assert "服务启动失败" in result["message"]
        assert "端口可能已被占用" in result["message"]
        assert "plugins.pendo.web_port" in result["message"]

    def test_web_stop_reports_external_running_server_without_failing(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url            = lambda: "http://127.0.0.1:8765",
                is_running         = lambda: True,
                is_managed_running = lambda: False,
                start              = lambda _db: False,
                stop               = lambda: False,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")

        handler = web_module.WebHandler(db=None)
        result = asyncio.run(
            handler.handle(
                "1001", "stop", context=types.SimpleNamespace(is_global_admin=lambda _uid: True)
            )
        )

        assert result["status"] == "success"
        assert "外部服务" in result["message"]

    def test_web_widget_token_sends_token_as_separate_private_message(self, monkeypatch):
        import importlib
        import sys
        import types

        monkeypatch.syspath_prepend(str(ROOT))
        monkeypatch.delitem(sys.modules, "plugins.pendo.handlers.web", raising=False)
        monkeypatch.setitem(
            sys.modules,
            "plugins.pendo.web.server",
            types.SimpleNamespace(
                get_url    = lambda: "http://127.0.0.1:8765",
                is_running = lambda: True,
                start      = lambda _db: True,
                stop       = lambda: True,
            ),
        )

        web_module = importlib.import_module("plugins.pendo.handlers.web")
        issuance   = {}

        def issue_widget(owner_id, *, expires_seconds, db):
            issuance.update(owner_id=owner_id, expires_seconds=expires_seconds, db=db)
            return "widget-token"

        monkeypatch.setattr(web_module, "generate_widget_token", issue_widget)

        actions = []

        async def send_action(action):
            actions.append(action)
            return True

        context = SimpleNamespace(send_action=send_action)
        handler = web_module.WebHandler(db=None)

        result = asyncio.run(handler.handle("1001", "widget-token", context=context))

        assert result["status"] == "success"
        assert "Widget Token 已单独私聊发送" in result["message"]
        assert "365 天" in result["message"]
        assert "widget-token" not in result["message"]
        assert issuance == {
            "owner_id": "1001",
            "expires_seconds": 365 * 24 * 60 * 60,
            "db": None,
        }
        assert len(actions) == 1
        token_text = actions[0]["params"]["message"][0]["data"]["text"]
        assert "Pendo Web Widget Token" in token_text
        assert "widget-token" in token_text

    def test_web_widget_revoke_revokes_only_callers_registered_tokens(self, monkeypatch):
        import importlib

        web_module = importlib.import_module("plugins.pendo.handlers.web")
        calls      = []
        db         = SimpleNamespace(
            revoke_widget_tokens=lambda owner_id: calls.append(owner_id) or 2,
        )
        handler = web_module.WebHandler(db=db)

        result = asyncio.run(handler.handle("1001", "widget-revoke", context=None))

        assert result == {
            "status": "success",
            "message": "✅ 已吊销 2 个 Widget Token；请重新生成并录入 Keychain",
        }
        assert calls == ["1001"]


class TestPendoSearchAndImportRegression:
    def test_search_handler_applies_date_field_for_range_filters(self, monkeypatch):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.handlers.search import SearchHandler

        monkeypatch.setattr(
            "plugins.pendo.utils.time_utils.now_in_timezone",
            lambda _user_id, _db: datetime(2026, 5, 3, 16, 30, 0),
        )

        calls = []

        class _ItemsRepo:
            def search_items_page(self, owner_id, query, filters, *, limit, offset):
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
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database

        db = Database(str(tmp_path / "pendo.db"))
        try:
            db.batch_insert_or_update(
                [
                    (
                        "insert",
                        {
                            "id": "note-1",
                            "type": "note",
                            "title": "脉冲星速记",
                            "content": "第一次导入内容",
                            "category": "研究",
                            "tags": ["memo"],
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
                            "id": "note-1",
                            "type": "note",
                            "title": "脉冲星速记",
                            "content": "更新后的导入内容",
                            "category": "研究",
                            "tags": ["memo"],
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


class TestPendoRedesignRegression:
    def test_widget_ledger_panel_prefers_amount_cents(self, tmp_path):
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database
        from plugins.pendo.web.api.widget import build_widget_summary

        db       = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-widget-ledger"
        try:
            db.insert_item(
                {
                    "id": "widget-ledger-expense",
                    "owner_id": owner_id,
                    "type": "ledger",
                    "title": "TEST_SCRIPTABLE 午饭",
                    "amount": 0,
                    "amount_cents": 12345,
                    "transaction_type": "expense",
                    "ledger_category": "餐饮",
                    "ledger_date": "2026-04-30",
                    "account_name": "微信",
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
        import sys
        from datetime import date

        sys.path.insert(0, str(ROOT))

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
        import sys

        sys.path.insert(0, str(ROOT))

        from plugins.pendo.services.db import Database

        db       = Database(str(tmp_path / "pendo.db"))
        owner_id = "u-search-collection"
        try:
            collection_id = db.create_event_collection(
                {
                    "id": "test_collection_category",
                    "owner_id": owner_id,
                    "kind": "multi_node",
                    "title": "TEST_STATS 学术会议",
                    "category": "工作",
                    "location": "上海",
                }
            )
            db.insert_item(
                {
                    "id": "test_collection_node",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "摘要截止",
                    "category": "",
                    "start_time": "2026-05-10T09:00:00",
                    "event_role": "multi_node_child",
                    "event_collection_id": collection_id,
                    "event_collection_kind": "multi_node",
                    "event_index": 1,
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
