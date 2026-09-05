"""日程批处理、日记和 Web 运行态。"""

from __future__ import annotations

import json

from tests.helpers.pendo_review_test_support import (
    Any,
    AsyncMock,
    Database,
    EventHandler,
    Path,
    SimpleNamespace,
    _seed_event_batch_fixture,
    asyncio,
    datetime,
    pytest,
    threading,
)


@pytest.mark.asyncio
async def test_event_collection_reminders_batch_all_child_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db                           = Database(str(tmp_path / "pendo-event-batch-family.db"))
    owner_id                     = "u-event-batch"
    event_ids                    = _seed_event_batch_fixture(db, owner_id)
    handler                      = EventHandler(db, SimpleNamespace(), SimpleNamespace())
    original_logs                = db.get_reminder_logs_by_item_ids
    batched_ids: list[list[str]] = []

    def counted_logs(request_owner: str, item_ids: list[str]):
        batched_ids.append(list(item_ids))
        return original_logs(request_owner, item_ids)

    def forbid_single_logs(*_args, **_kwargs):
        raise AssertionError("collection rendering must not query one child at a time")

    monkeypatch.setattr(db, "get_reminder_logs_by_item_ids", counted_logs)
    monkeypatch.setattr(db, "get_reminder_logs", forbid_single_logs)

    try:
        result = await handler.list_reminders(owner_id, "aaaabbbb", {})
        assert result["status"] == "success"
        assert len(batched_ids) == 1
        assert batched_ids[0] == event_ids[:3]
    finally:
        db.cleanup()


@pytest.mark.asyncio
async def test_event_list_batch_query_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db       = Database(str(tmp_path / "pendo-event-batch-responsive.db"))
    owner_id = "u-event-batch"
    _seed_event_batch_fixture(db, owner_id)
    handler              = EventHandler(db, SimpleNamespace(), SimpleNamespace())
    original_collections = db.get_event_collections_by_ids
    started              = threading.Event()
    release              = threading.Event()

    def blocking_collections(request_owner: str, collection_ids: list[str]):
        started.set()
        release.wait(timeout=0.5)
        return original_collections(request_owner, collection_ids)

    monkeypatch.setattr(db, "get_event_collections_by_ids", blocking_collections)
    task = asyncio.create_task(handler.list_events(owner_id, "2030-01-01..2030-01-02", {}))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert not task.done(), "the blocking DB call ran on the event-loop thread"
        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result["status"] == "success"
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        db.cleanup()


def test_event_collection_reminder_update_rolls_back_every_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """系列提醒任一节点失败时，节点和集合头都不得部分提交。"""
    db            = Database(str(tmp_path / "pendo-event-reminder-atomic.db"))
    owner_id      = "u-reminder-atomic"
    collection_id = "reminder-atomic"
    child_ids     = ["reminder-atomic-m01", "reminder-atomic-m02"]
    old_rules     = [{"offset_seconds": 0}]
    new_rules     = [{"offset_seconds": 3600}, {"offset_seconds": 0}]
    try:
        db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": "原子提醒",
                "reminder_rules": old_rules,
            }
        )
        for index, child_id in enumerate(child_ids, 1):
            start_time = f"2030-01-0{index}T10:00:00"
            db.insert_item(
                {
                    "id": child_id,
                    "owner_id": owner_id,
                    "type": "event",
                    "title": f"节点 {index}",
                    "start_time": start_time,
                    "remind_times": [start_time],
                    "reminder_rules": old_rules,
                    "event_role": "multi_node_child",
                    "event_collection_id": collection_id,
                    "event_collection_kind": "multi_node",
                }
            )

        original_sync = db._sync_reminder_logs
        sync_calls    = 0

        def fail_second_sync(cursor, item_id, remind_times):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                raise RuntimeError("注入系列提醒失败")
            original_sync(cursor, item_id, remind_times)

        monkeypatch.setattr(db, "_sync_reminder_logs", fail_second_sync)
        updates = {
            child_id: ([f"2030-01-0{index}T09:00:00"], new_rules)
            for index, child_id in enumerate(child_ids, 1)
        }
        with pytest.raises(RuntimeError, match="注入系列提醒失败"):
            db.update_event_collection_reminders(
                collection_id,
                owner_id,
                updates,
                new_rules,
            )

        for index, child_id in enumerate(child_ids, 1):
            child = db.get_item(child_id, owner_id)
            assert child.remind_times == [f"2030-01-0{index}T02:00:00+00:00"]
            assert child.reminder_rules == old_rules
        assert db.get_event_collection(collection_id, owner_id)["reminder_rules"] == old_rules
    finally:
        db.cleanup()


def test_delete_last_multi_node_child_and_undo_restores_whole_family(tmp_path: Path) -> None:
    db            = Database(str(tmp_path / "pendo-event-last-child-undo.db"))
    owner_id      = "u-last-child"
    collection_id = "last-child-collection"
    child_id      = "last-child-event"
    try:
        db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": owner_id,
                "kind": "multi_node",
                "title": "只有一个节点",
            }
        )
        db.insert_item(
            {
                "id": child_id,
                "owner_id": owner_id,
                "type": "event",
                "title": "唯一节点",
                "start_time": "2030-01-01T09:00:00",
                "event_role": "multi_node_child",
                "event_collection_id": collection_id,
                "event_collection_kind": "multi_node",
            }
        )

        assert db.delete_event_instance(child_id, owner_id) == ("唯一节点", True)
        assert db.get_event_collection(collection_id, owner_id) is None
        assert db.get_item(child_id, owner_id) is None

        restored = db.undo_delete(owner_id)
        assert restored["status"] == "success"
        assert restored["collection_id"] == collection_id
        assert db.get_event_collection(collection_id, owner_id)["title"] == "只有一个节点"
        assert db.get_item(child_id, owner_id).title == "唯一节点"
    finally:
        db.cleanup()


def test_event_range_uses_user_timezone_and_two_day_offset_prefilter(tmp_path: Path) -> None:
    db       = Database(str(tmp_path / "pendo-event-offset-range.db"))
    owner_id = "u-offset-range"
    try:
        db.update_user_settings(owner_id, {"timezone": "Etc/GMT+12"})
        db.insert_item(
            {
                "id": "extreme-offset-event",
                "owner_id": owner_id,
                "type": "event",
                "title": "跨 26 小时时区",
                # +14 的 1 月 3 日 00:30，在 UTC-12 仍是 1 月 1 日 22:30。
                "start_time": "2030-01-03T00:30:00+14:00",
            }
        )
        db.insert_item(
            {
                "id": "spanning-event",
                "owner_id": owner_id,
                "type": "event",
                "title": "跨午夜日程",
                "start_time": "2030-01-01T23:30:00-12:00",
                "end_time": "2030-01-02T01:00:00-12:00",
            }
        )

        first_day = db.get_events_for_range(
            owner_id,
            "2030-01-01T00:00:00",
            "2030-01-01T23:59:59",
        )
        second_day = db.get_events_for_range(
            owner_id,
            "2030-01-02T00:00:00",
            "2030-01-02T23:59:59",
        )

        assert "extreme-offset-event" in [event.id for event in first_day]
        assert "spanning-event" in [event.id for event in second_day]
    finally:
        db.cleanup()


def test_recurring_instances_and_time_only_edit_preserve_explicit_offset() -> None:
    start    = datetime.fromisoformat("2030-01-01T09:00:00+08:00")
    user_now = datetime.fromisoformat("2029-12-31T00:00:00+00:00")

    instances, exhausted = EventHandler._expand_recurring_instances(
        "FREQ=DAILY;COUNT=2",
        start,
        user_now,
    )
    event = SimpleNamespace(start_time="2030-01-01T09:00:00+08:00")

    assert exhausted is False
    assert [instance.isoformat() for instance in instances] == [
        "2030-01-01T09:00:00+08:00",
        "2030-01-02T09:00:00+08:00",
    ]
    assert EventHandler._normalize_datetime_candidate("11:30", event) == (
        "2030-01-01T11:30:00+08:00"
    )
    assert (
        EventHandler._extract_start_time_update(
            "改到2030-01-01 24:00",
            event,
        )
        == "2030-01-02T00:00:00+08:00"
    )


def test_event_wall_time_edit_rejects_dst_gap_and_fold() -> None:
    event = SimpleNamespace(
        start_time = "2030-03-09T14:00:00-05:00",
        timezone   = "America/New_York",
    )

    assert EventHandler._normalize_datetime_candidate("2030-03-10 02:30", event) is None
    assert EventHandler._normalize_datetime_candidate("2030-11-03 01:30", event) is None
    assert EventHandler._normalize_datetime_candidate("2030-03-10 03:30", event) == (
        "2030-03-10T03:30:00-04:00"
    )


@pytest.mark.asyncio
async def test_invalid_milestone_payload_returns_error_instead_of_crashing() -> None:
    handler = EventHandler(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    result = await handler.create_event(
        "u-invalid-milestone",
        {"title": "坏节点", "milestones": [{"name": "缺时间"}]},
        SimpleNamespace(),
    )

    assert result["status"] == "error"
    assert "第 1 个时间节点" in result["message"]


@pytest.mark.asyncio
async def test_far_future_event_reminder_and_corrupt_time_are_handled_safely(
    tmp_path: Path,
) -> None:
    db       = Database(str(tmp_path / "pendo-event-far-reminder.db"))
    owner_id = "u-far-reminder"
    try:
        with pytest.raises(ValueError, match="Invalid remind_times"):
            db.insert_item(
                {
                    "id": "rejected-corrupt-reminder",
                    "owner_id": owner_id,
                    "type": "event",
                    "title": "损坏提醒必须被写入端拒绝",
                    "start_time": "2030-12-31T10:00:00+08:00",
                    "remind_times": ["损坏时间"],
                }
            )
        db.insert_item(
            {
                "id": "far-reminder-event",
                "owner_id": owner_id,
                "type": "event",
                "title": "很久以后的日程",
                "start_time": "2030-12-31T10:00:00+08:00",
                "remind_times": ["2030-01-01T09:00:00+08:00"],
                "reminder_rules": [{"offset_seconds": 0}],
            }
        )
        stored_reminder = db.get_item("far-reminder-event", owner_id).remind_times[0]
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE items SET remind_times = ? WHERE id = ?",
                (json.dumps(["损坏时间", stored_reminder]), "far-reminder-event"),
            )
        db.cache_clear()
        handler = EventHandler(db, SimpleNamespace(), SimpleNamespace())

        listed    = await handler.list_reminders(owner_id, "2030-01-01", SimpleNamespace())
        confirmed = await handler.confirm_event_reminders(
            owner_id,
            "far-reminder-event all",
            SimpleNamespace(),
        )

        assert listed["status"] == "success"
        assert "很久以后的日程" in listed["message"]
        assert confirmed["status"] == "success"
        assert "已确认 1 个提醒" in confirmed["message"]
    finally:
        db.cleanup()


def test_event_id_detection_accepts_only_current_uuid_forms() -> None:
    handler = EventHandler(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    assert handler._looks_like_id("ABCDEF12")
    assert handler._looks_like_id("ABCDEF12ABCDEF12ABCDEF12ABCDEF12")
    assert not handler._looks_like_id("ABCDEF12ABCDEF12ABCDEF12ABCDEF12_M01")


def test_event_time_edit_rejects_ai_hallucinated_title() -> None:
    event = SimpleNamespace(title="原始标题")

    assert not EventHandler._should_apply_title_update(
        "时间改到2030-01-02 09:00",
        event,
        "AI 猜测的新标题",
    )
    assert EventHandler._should_apply_title_update(
        "用户明确写的新标题", event, "用户明确写的新标题"
    )


@pytest.mark.asyncio
async def test_diary_relative_dates_and_today_range_use_user_clock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from plugins.pendo.handlers.diary import DiaryHandler

    fixed_user_now = datetime.fromisoformat("2030-01-01T00:30:00-12:00")
    monkeypatch.setattr(
        "plugins.pendo.utils.time_utils.now_in_timezone",
        lambda _user_id, _db: fixed_user_now,
    )
    db = Database(str(tmp_path / "pendo-diary-user-clock.db"))
    try:
        handler = DiaryHandler(db)
        created = await handler.add_diary(
            "u-diary-clock",
            "今天 时区边界日记 mood:calm score:5",
            SimpleNamespace(),
        )
        db.insert_item(
            {
                "id": "server-day-diary",
                "owner_id": "u-diary-clock",
                "type": "diary",
                "title": "本机日期日记",
                "content": "不应出现在用户今天",
                "diary_date": "2026-07-14",
                "entry_time": "2026-07-14T10:00:00",
            }
        )

        listed = await handler.list_diaries(
            "u-diary-clock",
            "today",
            SimpleNamespace(),
        )
        invalid_mood = await handler.list_diaries(
            "u-diary-clock",
            "today mood:not-a-mood",
            SimpleNamespace(),
        )

        saved = db.get_item(created["item_id"], "u-diary-clock")
        assert created["status"] == "success"
        assert saved.diary_date == "2030-01-01"
        assert saved.entry_time == "2029-12-31T16:30:00+00:00"
        assert "时区边界日记" in listed["message"]
        assert "不应出现在用户今天" not in listed["message"]
        assert invalid_mood["status"] == "error"
        assert "Invalid diary mood" in invalid_mood["message"]
    finally:
        db.cleanup()


def test_diary_metadata_parser_is_case_insensitive_and_accumulates_tags() -> None:
    from plugins.pendo.handlers.diary import DiaryHandler

    handler = DiaryHandler(SimpleNamespace())
    parsed  = handler._parse_diary_text(
        'TAG:工作,复盘 tag:复盘,生活 WEATHER:"多云 转晴" FAVORITE:TRUE 今天完成整理。'
    )

    assert parsed == {
        "content": "今天完成整理。",
        "weather": "多云 转晴",
        "location": None,
        "mood": None,
        "mood_score": None,
        "tags": ["工作", "复盘", "生活"],
        "is_favorite": True,
    }


@pytest.mark.asyncio
async def test_diary_template_session_rejects_corrupt_progress() -> None:
    from plugins.pendo.handlers.diary import DiaryHandler

    class _Session:
        def __init__(self) -> None:
            self.data = {
                "prompts": ["第一题", "第二题"],
                "answers": [],
                "step": 1,
            }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def set(self, key, value):
            self.data[key] = value

    class _Context:
        ended = False

        async def end_session(self):
            self.ended = True
            return True

    context = _Context()
    result  = await DiaryHandler(SimpleNamespace()).handle_session_message(
        "u-corrupt",
        "不应写入",
        context,
        _Session(),
    )

    assert result == {"status": "error", "message": "❌ 模板会话状态损坏，请重新开始"}
    assert context.ended is True


@pytest.mark.asyncio
async def test_diary_template_session_uses_scoped_user_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.pendo.handlers.diary import DiaryHandler

    class _Session:
        def __init__(self) -> None:
            self.data = {
                "owner_id": "u-spoofed",
                "template_id": "review",
                "diary_date": "2030-01-01",
                "group_id": None,
                "prompts": ["今天做了什么？"],
                "answers": [],
                "step": 0,
            }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def set(self, key, value):
            self.data[key] = value

    class _Context:
        async def end_session(self):
            return True

    handler           = DiaryHandler(SimpleNamespace())
    handler.templates = {
        "review": {"name": "复盘", "prompts": ["今天做了什么？"]},
    }
    submit_result = AsyncMock(return_value={"status": "success", "message": "已保存"})
    monkeypatch.setattr(handler, "_submit_template_result", submit_result)
    context = _Context()

    result = await handler.handle_session_message(
        "u-scoped",
        "完成代码整理",
        context,
        _Session(),
    )

    assert result == {"status": "success", "message": "已保存"}
    submit_result.assert_awaited_once_with(
        "u-scoped",
        "2030-01-01",
        "review",
        ["今天做了什么？"],
        ["完成代码整理"],
        None,
        context,
    )


@pytest.mark.asyncio
async def test_diary_entries_on_same_day_have_deterministic_latest_first_order(
    tmp_path: Path,
) -> None:
    from plugins.pendo.handlers.diary import DiaryHandler

    db       = Database(str(tmp_path / "pendo-diary-order.db"))
    owner_id = "u-diary-order"
    try:
        for item_id, entry_time, content in (
            ("diary-early", "2030-01-01T08:00:00", "早间记录"),
            ("diary-late", "2030-01-01T22:00:00", "晚间记录"),
        ):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "diary",
                    "title": content,
                    "content": content,
                    "diary_date": "2030-01-01",
                    "entry_time": entry_time,
                }
            )

        result = await DiaryHandler(db).view_diary(
            owner_id,
            "2030-01-01",
            SimpleNamespace(),
        )

        assert result["status"] == "success"
        assert result["message"].index("晚间记录") < result["message"].index("早间记录")
    finally:
        db.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    ["token extra", "widget token extra", "start now", "stop now", "status now"],
)
async def test_web_handler_rejects_trailing_subcommand_arguments(
    monkeypatch: pytest.MonkeyPatch,
    args: str,
) -> None:
    """敏感命令只接受精确形式，尾随参数不得触发任何运行时动作。"""
    from plugins.pendo.handlers import web as web_module

    def unexpected_call(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("非精确 Web 子命令不应触发运行时动作")

    server = SimpleNamespace(
        get_url    = unexpected_call,
        is_running = unexpected_call,
        start      = unexpected_call,
        stop       = unexpected_call,
    )
    monkeypatch.setattr(web_module, "web_server", server)
    monkeypatch.setattr(web_module, "issue_login_code", unexpected_call)
    monkeypatch.setattr(web_module, "generate_widget_token", unexpected_call)

    result = await web_module.WebHandler(db=None).handle("1001", args, context=None)

    assert result["status"] == "error"
    assert "未知 Web 子命令" in result["message"]
    assert "可用命令" in result["message"]


@pytest.mark.asyncio
async def test_web_handler_fails_closed_when_runtime_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一可选组件缺失时均不得进入半可用状态。"""
    from plugins.pendo.handlers import web as web_module

    monkeypatch.setattr(web_module, "web_server", None)
    result = await web_module.WebHandler(db=None).handle("1001", "token", context=None)

    assert result["status"] == "error"
    assert "无法使用 Web UI" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_mode", ["rejected", "exception"])
async def test_web_private_delivery_failure_never_echoes_login_code(
    monkeypatch: pytest.MonkeyPatch,
    delivery_mode: str,
) -> None:
    """私聊 API 拒收或抛错时，公开回复均不得包含一次性登录码。"""
    from plugins.pendo.handlers import web as web_module

    server = SimpleNamespace(
        get_url    = lambda: "http://127.0.0.1:12001",
        is_running = lambda: True,
        start      = lambda _db: True,
        stop       = lambda: True,
    )

    async def fail_send(_action: dict[str, Any]) -> bool:
        if delivery_mode == "exception":
            raise RuntimeError("私聊发送失败")
        return False

    monkeypatch.setattr(web_module, "web_server", server)
    monkeypatch.setattr(
        web_module,
        "issue_login_code",
        lambda *_args, **_kwargs: "never-echo-this-code",
    )
    monkeypatch.setattr(
        web_module,
        "generate_widget_token",
        lambda *_args, **_kwargs: "unused-widget-token",
    )

    result = await web_module.WebHandler(db=None).handle(
        "1001",
        "token",
        context=SimpleNamespace(send_action=fail_send),
    )

    assert result["status"] == "error"
    assert "无法通过私聊安全发送凭据" in result["message"]
    assert "never-echo-this-code" not in result["message"]


@pytest.mark.asyncio
async def test_web_start_ignores_noncallable_error_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容未实现可调用错误读取器的旧服务替身。"""
    from plugins.pendo.handlers import web as web_module

    server = SimpleNamespace(
        get_url        = lambda: "http://127.0.0.1:12001",
        is_running     = lambda: False,
        start          = lambda _db: False,
        stop           = lambda: True,
        get_last_error = "not-callable",
    )
    monkeypatch.setattr(web_module, "web_server", server)
    monkeypatch.setattr(
        web_module,
        "issue_login_code",
        lambda *_args, **_kwargs: "unused-login-code",
    )
    monkeypatch.setattr(
        web_module,
        "generate_widget_token",
        lambda *_args, **_kwargs: "unused-widget-token",
    )

    result = await web_module.WebHandler(db=None).handle(
        "1001", "start", context=SimpleNamespace(is_global_admin=lambda _uid: True)
    )

    assert result["status"] == "error"
    assert "服务启动失败" in result["message"]
    assert "not-callable" not in result["message"]


def test_web_component_loader_only_downgrades_known_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """依赖缺失可降级，但插件内部模块缺失必须显式失败。"""
    from plugins.pendo.handlers import web as web_module

    def raise_missing_fastapi(_name: str) -> Any:
        raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")

    monkeypatch.setattr(web_module, "import_module", raise_missing_fastapi)
    assert web_module._load_web_components() is None

    def raise_missing_internal(_name: str) -> Any:
        raise ModuleNotFoundError(
            "No module named 'plugins.pendo.web.internal'",
            name="plugins.pendo.web.internal",
        )

    monkeypatch.setattr(web_module, "import_module", raise_missing_internal)
    with pytest.raises(ModuleNotFoundError, match="plugins.pendo.web.internal"):
        web_module._load_web_components()


@pytest.mark.asyncio
async def test_web_handler_offloads_blocking_runtime_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络探测、启停等待和密钥签发不得占用命令事件循环。"""
    from plugins.pendo.handlers import web as web_module

    event_loop_thread            = threading.get_ident()
    call_threads: dict[str, int] = {}
    running                      = False

    def is_running() -> bool:
        call_threads["is_running"] = threading.get_ident()
        return running

    def start(_db: Any) -> bool:
        nonlocal running
        call_threads["start"] = threading.get_ident()
        running               = True
        return True

    def stop() -> bool:
        nonlocal running
        call_threads["stop"] = threading.get_ident()
        running              = False
        return True

    def generate_widget(*_args: Any, **_kwargs: Any) -> str:
        call_threads["generate_widget"] = threading.get_ident()
        return "offloaded-widget-token"

    async def send_action(_action: dict[str, Any]) -> bool:
        return True

    server = SimpleNamespace(
        get_url            = lambda: "http://127.0.0.1:12001",
        is_running         = is_running,
        is_managed_running = lambda: running,
        start              = start,
        stop               = stop,
    )
    monkeypatch.setattr(web_module, "web_server", server)
    monkeypatch.setattr(
        web_module,
        "issue_login_code",
        lambda *_args, **_kwargs: "unused-login-code",
    )
    monkeypatch.setattr(web_module, "generate_widget_token", generate_widget)
    handler = web_module.WebHandler(db=None)
    context = SimpleNamespace(send_action=send_action, is_global_admin=lambda _uid: True)

    assert (await handler.handle("1001", "start", context=context))["status"] == "success"
    assert (await handler.handle("1001", "widget-token", context=context))["status"] == "success"
    assert (await handler.handle("1001", "status", context=context))["status"] == "success"
    assert (await handler.handle("1001", "stop", context=context))["status"] == "success"

    assert call_threads.keys() == {"is_running", "start", "generate_widget", "stop"}
    assert all(thread_id != event_loop_thread for thread_id in call_threads.values())


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.005, 101), (2.675, 268), (0.0049, 0), (0.0, 0)],
)
def test_web_amount_filter_uses_exact_ledger_rounding(value: float, expected: int) -> None:
    """金额筛选与账目落库共用四舍五入规则，并允许零作为筛选下界。"""
    from plugins.pendo.web.utils import amount_filter_cents

    assert amount_filter_cents(value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (float("nan"), "有限数值"),
        (float("inf"), "有限数值"),
        (float("-inf"), "有限数值"),
        (-10.0, "must not be negative"),
    ],
)
def test_web_amount_filter_rejects_invalid_values(value: float, message: str) -> None:
    from fastapi import HTTPException

    from plugins.pendo.web.utils import amount_filter_cents

    with pytest.raises(HTTPException, match=message) as exc_info:
        amount_filter_cents(value)
    assert exc_info.value.status_code == 422


def test_web_data_helpers_normalize_public_shapes(caplog: pytest.LogCaptureFixture) -> None:
    """集合只暴露白名单字段，条目序列化只接受映射结果。"""
    from plugins.pendo.web.utils import collection_payload, item_to_dict

    collection = {
        "id": "collection-1",
        "kind": "multi_node",
        "title": "会议",
        "category": "工作",
        "location": "线上",
        "notes": "备注",
        "owner_id": "private-owner",
    }
    assert collection_payload(collection) == {
        "id": "collection-1",
        "kind": "multi_node",
        "title": "会议",
        "category": "工作",
        "location": "线上",
        "notes": "备注",
        "display_id": "collection-1",
    }

    source = {"id": "item-1", 2: "numeric-key"}
    assert item_to_dict(source) == {
        "id": "item-1",
        "2": "numeric-key",
        "display_id": "item-1",
    }
    assert item_to_dict(SimpleNamespace(to_dict=lambda: {"id": "item-2"})) == {
        "id": "item-2",
        "display_id": "item-2",
    }
    assert item_to_dict(SimpleNamespace(to_dict="not-callable")) == {}
    assert item_to_dict(SimpleNamespace(to_dict=lambda: ["not", "a", "mapping"])) == {}
    assert [record.getMessage() for record in caplog.records] == [
        "Pendo item serialization skipped for type=SimpleNamespace",
        "Pendo item serializer returned non-mapping type=list",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2030-01-02", datetime(2030, 1, 2).date()),
        ("2030-01-02 23:59:58", datetime(2030, 1, 2).date()),
        ("2030-01-02T23:59:58Z", datetime(2030, 1, 2).date()),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_web_iso_date_parser_accepts_common_iso_forms(value: str, expected: Any) -> None:
    from plugins.pendo.web.utils import parse_iso_date

    assert parse_iso_date(value) == expected
