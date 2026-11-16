"""Pendo 配置发布、路由、导出、Web 与生命周期边界。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.pendo.utils import db_ops
from tests.helpers.pendo_test_support import (
    PluginCapabilities,
    attach_action_recorder,
    pendo_service_context,
    reset_pendo_runtime_config,
)
from tests.helpers.sqlite_test_support import trace_sql_statements


@pytest.fixture
def pendo_context(db, tmp_path):
    """真实服务集合（共用测试数据库）；私聊动作记录在 ``sent``，一律回执成功。"""

    with pendo_service_context(db, tmp_path, "test.pendo.runtime_boundaries") as context:
        context.sent = attach_action_recorder(context)
        yield context


class TestPendoRuntimeBoundaries:
    @pytest.mark.parametrize(
        ("invalid", "error_type"),
        [
            ({"web_enabled": 1}, TypeError),
            ({"web_host": ""}, ValueError),
            ({"web_host": " localhost "}, ValueError),
            ({"web_port": True}, ValueError),
            ({"web_port": 0}, ValueError),
            ({"web_port": 65_536}, ValueError),
            ({"web_session_cookie_secure": "true"}, TypeError),
            ({"web_demo_enabled": 0}, TypeError),
        ],
    )
    def test_runtime_config_validates_the_whole_generation_before_publish(
        self,
        invalid,
        error_type,
    ):
        from plugins.pendo.config import PendoConfig, PendoRuntimeSettings

        reset_pendo_runtime_config()
        expected = PendoRuntimeSettings(
            web_enabled               = False,
            web_host                  = "localhost",
            web_port                  = 12_003,
            web_session_cookie_secure = True,
            web_demo_enabled          = True,
        )
        assert PendoConfig.configure(
            {
                "web_enabled"              : expected.web_enabled,
                "web_host"                 : expected.web_host,
                "web_port"                 : expected.web_port,
                "web_session_cookie_secure": expected.web_session_cookie_secure,
                "web_demo_enabled"         : expected.web_demo_enabled,
            },
            settings_revision=10,
        )
        assert PendoConfig.runtime() == expected

        with pytest.raises(error_type):
            PendoConfig.configure(invalid, settings_revision=11)

        assert PendoConfig.runtime() == expected

    def test_runtime_config_rejects_stale_and_conflicting_revisions(self):
        from plugins.pendo.config import PendoConfig

        reset_pendo_runtime_config()
        assert PendoConfig.configure({"web_port": 12_010}, settings_revision=10)

        assert not PendoConfig.configure({"web_port": 12_009}, settings_revision=9)
        assert PendoConfig.runtime().web_port == 12_010

        with pytest.raises(RuntimeError, match="same revision"):
            PendoConfig.configure({"web_port": 12_011}, settings_revision=10)

        assert PendoConfig.runtime().web_port == 12_010

    def test_init_reads_demo_switch_from_global_config_and_updates_on_reload(
        self,
        monkeypatch,
        tmp_path,
    ):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.config import PendoConfig
        from plugins.pendo.web import server as web_server
        from tests.helpers.settings_snapshot import settings_snapshot

        class _DummySubscription:
            def __init__(self):
                self.callbacks = []

            def subscribe(self, callback):
                self.callbacks.append(callback)
                active = True

                def unsubscribe():
                    nonlocal active
                    if active:
                        active = False
                        self.callbacks.remove(callback)

                return unsubscribe

        subscription = _DummySubscription()
        web_starts   = []

        initial_snapshot = settings_snapshot(
            config={
                "plugins": {
                    "pendo": {
                        "web_enabled"     : False,
                        "web_demo_enabled": True,
                    }
                }
            },
            revision=1,
        )
        current_settings = {"value": initial_snapshot}
        context          = SimpleNamespace(
            config         = initial_snapshot.config,
            config_manager = None,
            capabilities=PluginCapabilities(config_subscription=subscription),
            state    = {},
            data_dir = tmp_path / "plugin-data" / "pendo",
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
            get_settings_snapshot=lambda: current_settings["value"],
        )

        # Web 监听是边界：不真的启动 uvicorn，只记录启动时已发布的端点。
        def fake_start(db):
            runtime = PendoConfig.runtime()
            web_starts.append((db, runtime.web_host, runtime.web_port))
            return True

        monkeypatch.setattr(web_server, "is_running", lambda: False)
        monkeypatch.setattr(web_server, "start", fake_start)
        reset_pendo_runtime_config()
        db_ops.set_database_singleton(None)

        pendo_main.init(context)
        try:
            database = pendo_main._runtime_service.database
            assert database is not None
            connection = database.get_connection()

            assert PendoConfig.runtime().web_demo_enabled is True
            assert len(subscription.callbacks) == 1
            assert Path(database.db_path) == context.data_dir / PendoConfig.DB_FILENAME
            before = PendoConfig.runtime()
            assert (before.web_enabled, before.web_host, before.web_port) == (
                False,
                "127.0.0.1",
                12001,
            )
            assert web_starts == []

            replacement_snapshot = settings_snapshot(
                config={
                    "plugins": {
                        "pendo": {
                            "web_enabled"     : True,
                            "web_host"        : "localhost",
                            "web_port"        : 12003,
                            "web_demo_enabled": False,
                        }
                    }
                },
                revision=2,
            )
            current_settings["value"] = replacement_snapshot
            asyncio.run(subscription.callbacks[0](replacement_snapshot.config))

            assert PendoConfig.runtime().web_demo_enabled is False
            after = PendoConfig.runtime()
            assert (after.web_enabled, after.web_host, after.web_port) == (
                True,
                "localhost",
                12003,
            )
            assert web_starts == [(database, "localhost", 12003)]

            pendo_main._cleanup_resources(context, stop_web=False)

            assert subscription.callbacks == []
            assert context.state["pendo_runtime"] == {}
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
            assert pendo_main._runtime_service.database is None
            assert db_ops.detach_database_singleton() is None
        finally:
            pendo_main._cleanup_resources(context, stop_web=False)

    def test_router_is_not_reused_across_group_contexts(self, db, pendo_context):
        from plugins.pendo import main as pendo_main

        router_g1 = pendo_main._build_command_router(pendo_context, group_id=1001)
        router_g2 = pendo_main._build_command_router(pendo_context, group_id=1002)

        assert router_g1 is not router_g2

        result_g1 = asyncio.run(router_g1.route("todo", "u1", "add 群一待办", pendo_context))
        result_g2 = asyncio.run(router_g2.route("todo", "u1", "add 群二待办", pendo_context))

        assert "群一待办" in result_g1["message"]
        assert "群二待办" in result_g2["message"]
        task_groups = {
            task.title: task.context["group_id"] for task in db.get_items("u1", {"type": "task"})
        }
        assert task_groups == {"群一待办": 1001, "群二待办": 1002}

    def test_import_command_guides_user_to_web_import(self, pendo_context):
        from plugins.pendo import main as pendo_main

        router = pendo_main._build_command_router(pendo_context)

        assert "import" in router.commands

        result = asyncio.run(router.route("import", "u1", "", pendo_context))

        assert result["status"] == "success"
        assert "/pendo web token" in result["message"]
        assert "Web 数据迁移页" in result["message"]
        assert "不接收本地文件路径" in result["message"]

    def test_plugin_trigger_aliases_are_routed_to_matching_subcommands(self, db, pendo_context):
        from plugins.pendo import main as pendo_main

        event = {"user_id": "u1"}

        asyncio.run(pendo_main.handle("待办", "add TEST_ALIAS_TASK", event, pendo_context))
        asyncio.run(
            pendo_main.handle("日程", "add 明天上午九点 TEST_ALIAS_EVENT", event, pendo_context)
        )
        asyncio.run(pendo_main.handle("日记", "add TEST_ALIAS_DIARY", event, pendo_context))

        items = {type(item).__name__: item for item in db.get_items("u1", {})}
        assert set(items) == {"TaskItem", "EventItem", "DiaryItem"}
        assert items["TaskItem"].title == "TEST_ALIAS_TASK"
        assert "TEST_ALIAS_EVENT" in items["EventItem"].title
        assert "TEST_ALIAS_DIARY" in items["DiaryItem"].content

    def test_handle_command_routing_preserves_multiline_note_body(self, db, pendo_context):
        from plugins.pendo import main as pendo_main

        args = "note add title:周末书单\n1. 三体\n2. 活着\ncat:其他 #阅读"

        result = asyncio.run(pendo_main._handle_command_routing("u1", args, pendo_context))

        [note] = db.get_items("u1", {"type": "note"})
        assert note.title == "周末书单"
        assert note.content == "1. 三体\n2. 活着"
        assert note.category == "其他"
        assert note.tags == ["阅读"]
        assert "周末书单" in result[0]["data"]["text"]

    def test_export_command_uploads_private_markdown_file(self, db, pendo_context):
        from plugins.pendo import main as pendo_main

        db.insert_item(
            {"owner_id": "1001", "type": "task", "title": "导出的待办", "status": "open"}
        )

        router = pendo_main._build_command_router(pendo_context)
        result = asyncio.run(
            router.route("export", "1001", "工作档案 last30d event,todo", pendo_context)
        )

        assert result["status"] == "success"
        assert "已通过 QQ 私聊文件发送给你" in result["message"]
        actions = pendo_context.sent
        assert len(actions) == 1
        assert actions[0]["action"] == "upload_private_file"
        assert actions[0]["params"]["user_id"] == 1001
        assert actions[0]["params"]["name"] == "工作档案.md"
        assert "导出的待办" in Path(actions[0]["params"]["file"]).read_text(encoding="utf-8")

    def test_export_month_week_and_type_combinations_use_list_style_ranges(
        self, tmp_path, monkeypatch, request
    ):
        import shutil
        from datetime import datetime as real_datetime

        from plugins.pendo.services.db import Database
        from plugins.pendo.services.exporter import ExporterService
        from plugins.pendo.utils import time_utils

        class FrozenDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                base = cls(2026, 5, 3, 16, 0, 0)
                return base if tz is None else base.replace(tzinfo=tz)

        monkeypatch.setattr(time_utils, "datetime", FrozenDateTime)
        db = Database(str(tmp_path / "pendo-export.db"))
        request.addfinalizer(db.cleanup)
        owner = "u-export-ranges"
        rows  = [
            {
                "id"              : "ld_may1",
                "owner_id"        : owner,
                "type"            : "ledger",
                "title"           : "五月一日账目",
                "ledger_category" : "餐饮",
                "transaction_type": "expense",
                "amount"          : 10,
                "amount_cents"    : 1000,
                "ledger_date"     : "2026-05-01",
                "created_at"      : "2026-05-01T12:00:00",
                "updated_at"      : "2026-05-01T12:00:00",
            },
            {
                "id"              : "ld_may3",
                "owner_id"        : owner,
                "type"            : "ledger",
                "title"           : "五月三日账目",
                "ledger_category" : "交通",
                "transaction_type": "expense",
                "amount"          : 20,
                "amount_cents"    : 2000,
                "ledger_date"     : "2026-05-03",
                "created_at"      : "2026-05-03T12:00:00",
                "updated_at"      : "2026-05-03T12:00:00",
            },
            {
                "id"              : "ld_may4",
                "owner_id"        : owner,
                "type"            : "ledger",
                "title"           : "五月四日账目",
                "ledger_category" : "交通",
                "transaction_type": "expense",
                "amount"          : 30,
                "amount_cents"    : 3000,
                "ledger_date"     : "2026-05-04",
                "created_at"      : "2026-05-04T12:00:00",
                "updated_at"      : "2026-05-04T12:00:00",
            },
            {
                "id"        : "ev_may1",
                "owner_id"  : owner,
                "type"      : "event",
                "title"     : "五月一日日程",
                "start_time": "2026-05-01T09:00:00",
                "end_time"  : "2026-05-01T10:00:00",
                "created_at": "2026-05-01T08:00:00",
                "updated_at": "2026-05-01T08:00:00",
            },
            {
                "id"        : "tk_may3",
                "owner_id"  : owner,
                "type"      : "task",
                "title"     : "五月三日待办",
                "status"    : "open",
                "priority"  : 1,
                "plan_date" : "2026-05-03",
                "created_at": "2026-05-03T09:00:00",
                "updated_at": "2026-05-03T09:00:00",
            },
            {
                "id"        : "note_may",
                "owner_id"  : owner,
                "type"      : "note",
                "title"     : "五月笔记",
                "content"   : "note",
                "created_at": "2026-05-02T10:00:00",
                "updated_at": "2026-05-02T10:00:00",
            },
        ]
        for row in rows:
            db.insert_item(row)

        service = ExporterService(db, tmp_path / "exports")

        month_result = service.export_markdown(owner, "本月账本 month ledger", {})
        month_text = Path(month_result["file_path"]).read_text(encoding="utf-8")
        assert month_result["record_count"] == 3
        assert "2026-05-01 00:00+08:00 .. 2026-05-31 23:59+08:00" in month_result["range_label"]
        assert "五月一日账目" in month_text
        assert "五月四日账目" in month_text

        week_result = service.export_markdown(owner, "本周账本 week ledger", {})
        week_text = Path(week_result["file_path"]).read_text(encoding="utf-8")
        assert week_result["record_count"] == 2
        assert "2026-04-27 00:00+08:00 .. 2026-05-03 23:59+08:00" in week_result["range_label"]
        assert "五月一日账目" in week_text
        assert "五月三日账目" in week_text
        assert "五月四日账目" not in week_text

        combo_result = service.export_markdown(owner, '"五月 工作" 2026-05 event,todo', {})
        assert combo_result["file_name"] == "五月 工作.md"
        assert combo_result["counts"]["event"] == 1
        assert combo_result["counts"]["task"] == 1
        assert combo_result["counts"]["note"] == 0

        shutil.rmtree(tmp_path / "exports", ignore_errors=True)

    def test_pendo_help_root_is_overview_and_subcommand_is_detailed(self):
        from plugins.pendo import main as pendo_main

        overview    = pendo_main._show_help("")
        export_help = pendo_main._show_help("export")

        assert "🧭 可用命令" in overview
        assert "• /pendo event" in overview
        assert "/pendo export <文件名>" not in overview
        assert "多节点事件会生成" not in overview
        assert "/pendo export <文件名> [范围] [类型]" in export_help
        assert "week(本周), month(本月)" in export_help

    def test_cleanup_clears_pendo_runtime_state(self):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.utils import db_ops

        class _DummyDb:
            def __init__(self):
                self.cleaned = False

            def cleanup(self):
                self.cleaned = True

        db = _DummyDb()
        db_ops.set_database_singleton(db)

        context = SimpleNamespace(
            state={"pendo_runtime": {"services": {"x": 1}, "router": object()}},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        pendo_main.cleanup(context)

        assert db.cleaned is True
        assert context.state["pendo_runtime"] == {}

    def test_cleanup_does_not_create_pendo_runtime_when_absent(self):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.utils import db_ops

        class _DummyDb:
            def cleanup(self):
                return None

        db_ops.set_database_singleton(_DummyDb())

        context = SimpleNamespace(
            state={},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        pendo_main.cleanup(context)

        assert "pendo_runtime" not in context.state

    def test_get_database_reuses_shared_singleton_across_contexts(self):
        from plugins.pendo.services.db import Database
        from plugins.pendo.utils import db_ops

        db = Database(":memory:")
        try:
            db_ops.set_database_singleton(db)
            ctx_a = SimpleNamespace()
            ctx_b = SimpleNamespace()

            assert db_ops.get_database(ctx_a) is db
            assert db_ops.get_database(ctx_b) is db
        finally:
            db.cleanup()
            db_ops.set_database_singleton(None)

    def test_get_database_uses_context_data_dir_when_singleton_is_empty(self, tmp_path):
        from plugins.pendo.config import PendoConfig
        from plugins.pendo.utils import db_ops

        context = SimpleNamespace(data_dir=tmp_path / "runtime" / "pendo")
        db_ops.set_database_singleton(None)
        db = db_ops.get_database(context)
        try:
            assert Path(db.db_path) == context.data_dir / PendoConfig.DB_FILENAME
            assert context.data_dir.is_dir()
        finally:
            database = db_ops.detach_database_singleton()
            if database is not None:
                database.cleanup()

    def test_cached_empty_values_do_not_fall_through_to_sql(self):
        from plugins.pendo.services.db import Database

        db = Database(":memory:")
        try:
            settings_key     = db._cache_key("settings", "u-empty")
            resolved_filters = db._resolve_item_filters("u-empty", {"type": "note"})
            items_key        = db._cache_key("items", "u-empty", resolved_filters, 10, 0)
            db._cache_set(settings_key, {})
            db._cache_set(items_key, [])

            # 在 SQLite 连接上记录实际执行的语句，缓存命中时不应有任何查询。
            with trace_sql_statements(db.get_connection()) as statements:
                assert db.get_user_settings("u-empty") == {}
                assert db.get_items("u-empty", {"type": "note"}, 10, 0) == []
                assert statements == []

                # 对照：没有缓存的用户确实会查库，说明上面的记录是有效的。
                db.get_user_settings("u-uncached")
                assert statements
        finally:
            db.cleanup()

    def test_start_web_server_restarts_existing_server(self, monkeypatch):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.web import server as web_server

        calls = []
        state = {"running": True}
        db    = object()

        monkeypatch.setattr(web_server, "is_running", lambda: state["running"])

        def fake_stop():
            calls.append("stop")
            state["running"] = False
            return True

        def fake_start(start_db):
            calls.append(("start", start_db))
            state["running"] = True
            return True

        monkeypatch.setattr(web_server, "stop", fake_stop)
        monkeypatch.setattr(web_server, "start", fake_start)

        assert pendo_main._start_web_server(db) is True
        assert calls == ["stop", ("start", db)]
        assert state["running"] is True

    def test_start_web_server_does_not_start_when_old_server_cannot_stop(self, monkeypatch):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.web import server as web_server

        calls = []
        state = {"running": True}

        monkeypatch.setattr(web_server, "is_running", lambda: state["running"])

        def fake_stop():
            calls.append("stop")
            return False

        def fake_start(_db):
            calls.append("start")
            return True

        monkeypatch.setattr(web_server, "stop", fake_stop)
        monkeypatch.setattr(web_server, "start", fake_start)

        assert pendo_main._start_web_server(object()) is False
        assert calls == ["stop"]

    def test_shutdown_stops_web_and_cleans_owned_pendo_database(self, monkeypatch):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.services.runtime import PendoRuntimeService
        from plugins.pendo.web import server as web_server

        class _DummyDb:
            def __init__(self):
                self.cleaned = False

            def cleanup(self):
                self.cleaned = True

        database        = _DummyDb()
        runtime_service = PendoRuntimeService()
        runtime_service.adopt_database(database)
        stopped = []

        # Web 监听是边界：不真的停 uvicorn，只记录停止请求。
        def fake_stop(*_args, **_kwargs):
            stopped.append("web")
            return True

        monkeypatch.setattr(web_server, "stop", fake_stop)

        context = SimpleNamespace(
            state={
                "pendo_runtime": {
                    "services"         : {"x": 1},
                    "router"           : object(),
                    "lifecycle_service": runtime_service,
                }
            },
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )

        asyncio.run(pendo_main.shutdown(context))

        assert stopped == ["web"]
        assert database.cleaned is True
        assert runtime_service.database is None
        assert context.state["pendo_runtime"] == {}

    @pytest.mark.parametrize("asynchronous", [False, True])
    def test_cleanup_retains_resources_until_web_stops(self, monkeypatch, asynchronous):
        from plugins.pendo import main as pendo_main
        from plugins.pendo.services.runtime import PendoRuntimeService
        from plugins.pendo.web import server as web_server

        cleaned = []
        database = SimpleNamespace(cleanup=lambda: cleaned.append("db"))
        service = PendoRuntimeService()
        service.adopt_database(database)
        stopped = [False]

        # Web 监听是边界：停不掉时 stop() 为假且服务线程仍在运行。
        monkeypatch.setattr(web_server, "stop", lambda *_args, **_kwargs: stopped[0])
        monkeypatch.setattr(web_server, "is_managed_running", lambda: not stopped[0])
        runtime = {"services": {"ready": True}, "lifecycle_service": service}
        context = SimpleNamespace(
            state={"pendo_runtime": runtime},
            logger=SimpleNamespace(warning=lambda *args: None, info=lambda *args: None),
        )

        def cleanup():
            if asynchronous:
                asyncio.run(pendo_main.shutdown(context))
            else:
                pendo_main.cleanup(context)

        cleanup()
        assert cleaned == []
        assert service.database is database
        assert runtime["services"] == {"ready": True}
        stopped[0] = True
        cleanup()
        assert cleaned == ["db"]
        assert service.database is None
        assert runtime == {}

    def test_run_scheduled_task_swallows_cancelled_error(self):
        from plugins.pendo import main as pendo_main

        metrics     : list[tuple[str, float, bool]] = []
        log_messages: list[str]                     = []
        log = SimpleNamespace(info=lambda msg, *args: log_messages.append(msg % args))

        class _Metrics:
            async def record_plugin_execution(self, plugin, name, duration, *, is_error=False):
                assert plugin == "pendo"
                metrics.append((name, duration, is_error))

        context = SimpleNamespace(metrics=_Metrics())

        async def cancelled_task():
            raise asyncio.CancelledError()

        result = asyncio.run(
            pendo_main._run_scheduled_task(context, "daily_briefings", cancelled_task, log)
        )

        assert result == []
        assert metrics and metrics[0][0] == "scheduled.daily_briefings"
        assert metrics[0][2] is False
        assert any("cancelled during shutdown" in message for message in log_messages)

    def test_task_status_pagination_page_two_spans_categories(self, db):
        from plugins.pendo.handlers.task import TaskHandler

        for category, prefix, count in [("2026-02-10", "d", 8), ("work", "w", 7)]:
            for index in range(1, count + 1):
                db.insert_item(
                    {
                        "id"        : f"{prefix}{index}",
                        "owner_id"  : "u1",
                        "type"      : "task",
                        "title"     : f"{prefix}{index}",
                        "category"  : category,
                        "status"    : "done",
                        "created_at": f"2026-02-10T08:00:0{index}",
                    }
                )
        handler = TaskHandler(db)

        result = asyncio.run(handler.list_tasks("u1", "done page:2", {}))

        assert result["status"] == "success"
        message = result["message"]
        assert "📂 work" in message
        assert "w3" in message
        assert "w7" in message
        assert "w1" not in message
