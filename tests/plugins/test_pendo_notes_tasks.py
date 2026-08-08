"""笔记和任务命令回归。"""

from __future__ import annotations

from tests.helpers.pendo_review_test_support import (
    Database,
    EventHandler,
    NoteHandler,
    Path,
    ReminderService,
    SimpleNamespace,
    TaskHandler,
    _make_temp_db,
    asyncio,
    datetime,
    pytest,
    shutil,
)


def test_note_list_accepts_bare_time_range_before_category_inference(monkeypatch):
    from plugins.pendo.utils import time_utils

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 5, 3, 16, 30, 0)
            return current if tz is None else current.replace(tzinfo=tz)

    monkeypatch.setattr(time_utils, "datetime", _FrozenDateTime)

    temp_dir, db = _make_temp_db("pendo_review_note_bare_range")
    owner_id = "u-note-bare-range"

    try:
        for item_id, created_at, title in [
            ("note-apr", "2026-04-30T10:00:00", "四月 RustDesk"),
            ("note-may", "2026-05-02T10:00:00", "五月 RustDesk"),
            ("note-jun", "2026-06-01T10:00:00", "六月 RustDesk"),
        ]:
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "note",
                    "title": title,
                    "content": "密钥内容",
                    "category": "密钥",
                    "tags": ["rustdesk"],
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )

        result = asyncio.run(
            NoteHandler(db).list_notes(owner_id, "month cat:密钥 #rustdesk", SimpleNamespace())
        )

        assert result["status"] == "success"
        assert "时间: month" in result["message"]
        assert "分类: 密钥" in result["message"]
        assert "标签: #rustdesk" in result["message"]
        assert "分类: month" not in result["message"]
        assert "五月 RustDesk" in result["message"]
        assert "四月 RustDesk" not in result["message"]
        assert "六月 RustDesk" not in result["message"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_event_edit_explicit_fields_do_not_become_title_without_ai():
    class _FallbackTitleParser:
        async def parse_event_with_ai(self, *_args, **_kwargs):
            raise RuntimeError("no ai")

        def parse_natural_language(self, text, _user_id):
            return {"title": text}

        def build_remind_times_from_offsets(self, _start_time, _offsets):
            return []

        def build_reminder_rules_from_description(self, _description):
            return []

    temp_dir, db = _make_temp_db("pendo_review_event_explicit_edit")
    owner_id = "u-event-explicit-edit"

    def insert_event(item_id: str):
        db.insert_item(
            {
                "id": item_id,
                "owner_id": owner_id,
                "type": "event",
                "title": "原始日程",
                "content": "",
                "category": "工作",
                "location": "上海",
                "notes": "原备注",
                "tags": [],
                "start_time": "2026-05-04T09:00:00",
                "end_time": "2026-05-04T10:00:00",
                "remind_times": ["2026-05-04T09:00:00"],
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

    try:
        handler = EventHandler(db, _FallbackTitleParser(), ReminderService(db))

        insert_event("evloc001")
        result = asyncio.run(
            handler.edit_event(owner_id, "evloc001 地点改到北京南", SimpleNamespace())
        )
        assert result["status"] == "success"
        event = db.get_item("evloc001", owner_id)
        assert event.title == "原始日程"
        assert event.location == "北京南"
        assert event.notes == "原备注"
        assert event.start_time == "2026-05-04T01:00:00+00:00"

        insert_event("evnote01")
        result = asyncio.run(
            handler.edit_event(owner_id, "evnote01 备注为从北京南坐G123去会场", SimpleNamespace())
        )
        assert result["status"] == "success"
        event = db.get_item("evnote01", owner_id)
        assert event.title == "原始日程"
        assert event.location == "上海"
        assert event.notes == "从北京南坐G123去会场"
        assert event.start_time == "2026-05-04T01:00:00+00:00"

        insert_event("evtitle1")
        result = asyncio.run(
            handler.edit_event(owner_id, "evtitle1 标题改为FAST会议行程", SimpleNamespace())
        )
        assert result["status"] == "success"
        event = db.get_item("evtitle1", owner_id)
        assert event.title == "FAST会议行程"
        assert event.location == "上海"
        assert event.notes == "原备注"
        assert event.start_time == "2026-05-04T01:00:00+00:00"

        insert_event("evtime01")
        result = asyncio.run(
            handler.edit_event(owner_id, "evtime01 改到2026-05-04 24:00", SimpleNamespace())
        )
        assert result["status"] == "success"
        event = db.get_item("evtime01", owner_id)
        assert event.title == "原始日程"
        assert event.location == "上海"
        assert event.notes == "原备注"
        assert event.start_time == "2026-05-04T16:00:00+00:00"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_note_edit_only_updates_explicit_metadata_when_no_body():
    temp_dir, db = _make_temp_db("pendo_review_note_edit_partial")
    owner_id = "u-note-edit"

    try:
        db.insert_item(
            {
                "id": "note-edit",
                "owner_id": owner_id,
                "type": "note",
                "title": "原始标题",
                "content": "原始正文",
                "category": "旧分类",
                "tags": ["旧标签"],
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        result = asyncio.run(
            NoteHandler(db).edit_note(owner_id, "note-edit cat:新分类 #新标签", SimpleNamespace())
        )

        assert result["status"] == "success"
        note = db.get_item("note-edit", owner_id)
        assert note.title == "原始标题"
        assert note.content == "原始正文"
        assert note.category == "新分类"
        assert note.tags == ["新标签"]
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_note_list_control_tokens_are_exact_and_conflicts_are_rejected():
    user_now = datetime(2026, 1, 15, 12, 0, 0)

    tag_filter = NoteHandler._parse_list_filters("#all", user_now)
    assert tag_filter.tag == "all"
    assert tag_filter.show_all is False

    category_filter = NoteHandler._parse_list_filters('cat:"工作 空间"', user_now)
    assert category_filter.category == "工作 空间"

    first_page = NoteHandler._parse_list_filters("page:1", user_now)
    assert first_page.page == 1
    assert first_page.page_explicit is True

    for invalid in (
        "all page:1",
        "page:1 page:2",
        "all all",
        "month week",
        "since:month week",
    ):
        with pytest.raises(ValueError):
            NoteHandler._parse_list_filters(invalid, user_now)


def test_note_list_uses_user_clock_and_counts_all_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixed_now = datetime(2026, 1, 15, 12, 0, 0)
    monkeypatch.setattr(
        "plugins.pendo.utils.time_utils.now_in_timezone",
        lambda _user_id, _db: fixed_now,
    )
    db = Database(str(tmp_path / "pendo-note-list-clock.db"))
    owner_id = "note-list-clock"

    try:
        for index in range(55):
            created_at = "2025-12-31T23:30:00" if index == 0 else "2026-01-02T09:00:00"
            db.insert_item(
                {
                    "id": f"clock-note-{index:02d}",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": f"时区笔记-{index:02d}",
                    "content": "正文",
                    "category": "甲类" if index < 30 else "乙类",
                    "tags": ["focus"] if index < 2 else [],
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )

        handler = NoteHandler(db)
        overview = asyncio.run(handler.list_notes(owner_id, "", SimpleNamespace()))
        first_page = asyncio.run(handler.list_notes(owner_id, "page:1", SimpleNamespace()))
        monthly = asyncio.run(handler.list_notes(owner_id, "month #focus", SimpleNamespace()))

        assert overview["status"] == "success"
        assert "(共55项)" in overview["message"]
        assert "**甲类** (30项)" in overview["message"]
        assert "**乙类** (25项)" in overview["message"]
        assert "(第1页)" in first_page["message"]
        assert "分类概览" not in first_page["message"]
        assert "时区笔记-54" in first_page["message"]
        assert monthly["status"] == "success"
        assert "时区笔记-01" in monthly["message"]
        assert "时区笔记-00" not in monthly["message"]
        assert "分类概览" not in monthly["message"]
    finally:
        db.cleanup()


def test_note_create_and_view_timestamps_use_user_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixed_now = datetime(2031, 2, 3, 4, 5, 6)
    monkeypatch.setattr(
        "plugins.pendo.utils.time_utils.now_in_timezone",
        lambda _user_id, _db: fixed_now,
    )
    db = Database(str(tmp_path / "pendo-note-write-clock.db"))
    owner_id = "note-write-clock"

    try:
        handler = NoteHandler(db)
        created = asyncio.run(
            handler.create_note(
                owner_id,
                "title:时区笔记 content 正文",
                SimpleNamespace(),
            )
        )
        note = db.get_item(created["item_id"], owner_id)
        assert note.created_at == "2031-02-02T20:05:06+00:00"
        assert note.updated_at == "2031-02-02T20:05:06+00:00"
        before_view = (note.updated_at, note.version)

        viewed = asyncio.run(handler.view_note(owner_id, created["item_id"], SimpleNamespace()))
        assert viewed["status"] == "success"
        viewed_note = db.get_item(created["item_id"], owner_id)
        assert viewed_note.last_viewed == "2031-02-02T20:05:06+00:00"
        assert (viewed_note.updated_at, viewed_note.version) == before_view
    finally:
        db.cleanup()


def test_note_parser_supports_quoted_fields_and_rejects_malformed_metadata(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "pendo-note-parser.db"))
    owner_id = "note-parser"

    try:
        handler = NoteHandler(db)
        prefixed = handler._parse_note_text("title:“中文 标题” content 正文 cat:'工作 空间' #标签")
        assert prefixed["title"] == "中文 标题"
        assert prefixed["content"] == "正文"
        assert prefixed["category"] == "工作 空间"

        inline = handler._parse_note_text("正文 title:'行内 标题' cat:“分类 空间”")
        assert inline["title"] == "行内 标题"
        assert inline["content"] == "正文"
        assert inline["category"] == "分类 空间"

        empty_content = handler._parse_note_text('title:"仅标题" content')
        assert empty_content["title"] == "仅标题"
        assert empty_content["content"] == ""
        assert handler._parse_note_text('title:"标题" contentious')["content"] == "contentious"

        malformed_inputs = (
            "title:",
            'title:""',
            'title:"未闭合',
            "正文 title:",
            "正文 title:'未闭合",
            "正文 cat:",
            '正文 ref:""',
            '正文 ref:"" 后续',
            "正文 cat:一 cat:二",
            "正文\ncat:一\ncat:二",
            '正文 cat:"未闭合 分类',
        )
        for content in malformed_inputs:
            result = asyncio.run(handler.create_note(owner_id, content, SimpleNamespace()))
            assert result["status"] == "error"

        assert db.get_items(owner_id, filters={"type": "note"}) == []
    finally:
        db.cleanup()


def test_note_noop_mutations_and_link_arguments_do_not_write(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "pendo-note-noop.db"))
    owner_id = "note-noop"

    try:
        db.insert_item(
            {
                "id": "note-source",
                "owner_id": owner_id,
                "type": "note",
                "title": "来源",
                "content": "正文",
                "category": "资料",
                "tags": ["已有"],
                "references": [
                    {
                        "kind": "item",
                        "id": "note-target",
                        "type": "note",
                        "title": "目标",
                    }
                ],
                "related_items": ["note-target"],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        for item_id, category in (
            ("note-target", "资料"),
            ("note-spaced", "工作 空间"),
            ("note-safe", "保留"),
        ):
            db.insert_item(
                {
                    "id": item_id,
                    "owner_id": owner_id,
                    "type": "note",
                    "title": item_id,
                    "content": "正文",
                    "category": category,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-01T00:00:00",
                }
            )

        handler = NoteHandler(db)
        duplicate_tag = asyncio.run(
            handler.tag_note(owner_id, "note-source #已有", SimpleNamespace())
        )
        missing_tag = asyncio.run(
            handler.untag_note(owner_id, "note-source #不存在", SimpleNamespace())
        )
        duplicate_link = asyncio.run(
            handler.link_note(owner_id, "note-source note-target", SimpleNamespace())
        )
        duplicate_category_edit = asyncio.run(
            handler.edit_note(owner_id, "note-source cat:资料", SimpleNamespace())
        )
        duplicate_reference_edit = asyncio.run(
            handler.edit_note(owner_id, "note-source ref:note-target", SimpleNamespace())
        )
        noop_append = asyncio.run(
            handler.append_note(owner_id, "note-source " + chr(1), SimpleNamespace())
        )
        self_link = asyncio.run(
            handler.link_note(owner_id, "note-source note-source", SimpleNamespace())
        )
        extra_targets = asyncio.run(
            handler.link_note(
                owner_id,
                "note-source note-target note-safe",
                SimpleNamespace(),
            )
        )

        assert duplicate_tag["status"] == "info"
        assert missing_tag["status"] == "info"
        assert duplicate_link["status"] == "info"
        assert duplicate_category_edit["status"] == "warning"
        assert duplicate_reference_edit["status"] == "warning"
        assert noop_append["status"] == "warning"
        assert self_link["status"] == "error"
        assert extra_targets["status"] == "error"
        assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0

        unsafe_delete = asyncio.run(
            handler.delete_note(owner_id, "cat:资料 unexpected", SimpleNamespace())
        )
        assert unsafe_delete["status"] == "error"
        assert db.get_item("note-source", owner_id) is not None
        assert db.get_item("note-target", owner_id) is not None

        quoted_delete = asyncio.run(
            handler.delete_note(owner_id, 'cat:"工作 空间"', SimpleNamespace())
        )
        assert quoted_delete["status"] == "success"
        assert db.get_item("note-spaced", owner_id) is None
        assert db.get_item("note-safe", owner_id) is not None
    finally:
        db.cleanup()


def test_note_link_enforces_reference_limit_before_writing(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "pendo-note-reference-limit.db"))
    owner_id = "note-reference-limit"
    references = [
        {"kind": "item", "id": f"old-{index:03d}", "type": "note", "title": "旧引用"}
        for index in range(100)
    ]

    try:
        db.insert_item(
            {
                "id": "limit-source",
                "owner_id": owner_id,
                "type": "note",
                "title": "已满引用",
                "content": "正文",
                "references": references,
                "related_items": [reference["id"] for reference in references],
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        db.insert_item(
            {
                "id": "limit-target",
                "owner_id": owner_id,
                "type": "note",
                "title": "新目标",
                "content": "正文",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )

        result = asyncio.run(
            NoteHandler(db).link_note(
                owner_id,
                "limit-source limit-target",
                SimpleNamespace(),
            )
        )

        assert result["status"] == "error"
        assert "100" in result["message"]
        assert len(db.get_item("limit-source", owner_id).references) == 100
        assert db.get_connection().execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0] == 0
    finally:
        db.cleanup()


def test_note_backlinks_are_newest_first_and_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(str(tmp_path / "pendo-note-backlinks.db"))
    owner_id = "note-backlinks"

    try:
        db.insert_item(
            {
                "id": "backlink-source",
                "owner_id": owner_id,
                "type": "note",
                "title": "被引用笔记",
                "content": "正文",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )
        for index in range(12):
            timestamp = f"2026-01-{index + 1:02d}T12:00:00"
            db.insert_item(
                {
                    "id": f"back-{index:02d}",
                    "owner_id": owner_id,
                    "type": "note",
                    "title": f"反向引用 {index}",
                    "content": "正文",
                    "related_items": ["backlink-source"],
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            )

        monkeypatch.setattr(
            db,
            "get_all_items",
            lambda *_args, **_kwargs: pytest.fail("backlink lookup must not scan all notes"),
        )
        backlinks = asyncio.run(NoteHandler(db)._find_note_backlinks(owner_id, "backlink-source"))
        assert [note.id for note in backlinks] == [
            f"back-{index:02d}" for index in range(11, 1, -1)
        ]
    finally:
        db.cleanup()


def test_task_list_supports_cat_tag_and_date_filters():
    from plugins.pendo.models.item import TaskStatus

    temp_dir, db = _make_temp_db("pendo_review_task_list_filters")
    owner_id = "u-task-list-filters"

    try:
        db.insert_item(
            {
                "id": "task-work",
                "owner_id": owner_id,
                "type": "task",
                "title": "CmdAudit 写周报",
                "category": "工作",
                "tags": ["cmdaudit", "周报"],
                "plan_date": "2026-05-10",
                "deadline_at": "2026-05-11T18:00:00",
                "priority": 2,
                "status": TaskStatus.OPEN.value,
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        db.insert_item(
            {
                "id": "task-home",
                "owner_id": owner_id,
                "type": "task",
                "title": "CmdAudit 买礼物",
                "category": "家庭",
                "tags": ["family"],
                "plan_date": "2026-05-12",
                "priority": 4,
                "status": TaskStatus.OPEN.value,
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )

        handler = TaskHandler(db)

        by_cat = asyncio.run(handler.list_tasks(owner_id, "cat:工作", SimpleNamespace()))
        assert by_cat["status"] == "success"
        assert "task-work" in by_cat["message"]
        assert "task-home" not in by_cat["message"]

        by_tag = asyncio.run(handler.list_tasks(owner_id, "#cmdaudit", SimpleNamespace()))
        assert by_tag["status"] == "success"
        assert "task-work" in by_tag["message"]
        assert "task-home" not in by_tag["message"]

        by_date = asyncio.run(handler.list_tasks(owner_id, "2026-05-10", SimpleNamespace()))
        assert by_date["status"] == "success"
        assert "task-work" in by_date["message"]
        assert "task-home" not in by_date["message"]

        bad_page = asyncio.run(handler.list_tasks(owner_id, "工作 page:x", SimpleNamespace()))
        assert bad_page["status"] == "error"
    finally:
        db.cleanup()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_task_inline_parser_supports_quotes_clearing_and_stable_tag_deduplication():
    handler = TaskHandler(SimpleNamespace())

    parsed = handler._parse_task_text(
        '"Write weekly report" cat:"Deep Work" plan:none deadline:clear '
        'remind:"2026-05-01T09:00, 2026-05-02T10:00" p:2 #Focus #focus',
        "task-parser-user",
        apply_defaults=False,
    )

    assert parsed["title"] == "Write weekly report"
    assert parsed["category"] == "Deep Work"
    assert parsed["plan_date"] is None
    assert parsed["deadline_at"] is None
    assert parsed["remind_times"] == ["2026-05-01T09:00", "2026-05-02T10:00"]
    assert parsed["priority"] == 2
    assert parsed["tags"] == ["Focus"]
    assert parsed["_explicit_fields"] == {
        "plan_date": True,
        "deadline_at": True,
        "remind_times": True,
        "category": True,
        "priority": True,
        "tags": True,
    }

    apostrophe_title = handler._parse_task_text(
        "Review O'Reilly guide",
        "task-parser-user",
        apply_defaults=False,
    )
    assert apostrophe_title["title"] == "Review O'Reilly guide"
    quoted_apostrophe_title = handler._parse_task_text(
        '"Review O\'Reilly guide"',
        "task-parser-user",
        apply_defaults=False,
    )
    assert quoted_apostrophe_title["title"] == "Review O'Reilly guide"

    # 字段和标签必须是完整 token；URL 片段、单词内部的 # 和相似字段名均属于标题。
    boundary_text = "scat:工作 reportp:2 notype=event"
    boundary = handler._parse_task_text(
        boundary_text,
        "task-parser-user",
        apply_defaults=False,
    )
    assert boundary["title"] == boundary_text
    assert boundary["category"] == "未分类"
    assert boundary["priority"] == 3
    assert boundary["tags"] == []

    inline_text = "链接 https://example.test/page#fragment 和 inline#tag 不是标签"
    inline = handler._parse_task_text(
        inline_text,
        "task-parser-user",
        apply_defaults=False,
    )
    assert inline["title"] == inline_text
    assert inline["tags"] == []


@pytest.mark.parametrize(
    "args",
    [
        "标题 plan:2026-05-01 date:2026-05-02",
        "标题 cat:",
        "标题 remind:",
        "标题 p:0",
        "标题 #" + "x" * 21,
        "x" * 201,
        '标题 cat:"未闭合',
        "标题 " + chr(1),
        "x" * 5_001,
    ],
)
def test_task_inline_parser_rejects_ambiguous_or_unsafe_input(args: str) -> None:
    with pytest.raises(ValueError):
        TaskHandler(SimpleNamespace())._parse_task_text(
            args,
            "task-parser-user",
            apply_defaults=False,
        )


def test_task_add_rejects_metadata_only_input_without_creating_placeholder_title(tmp_path: Path):
    db = Database(str(tmp_path / "pendo-task-title-required.db"))
    owner_id = "task-title-required"
    try:
        result = asyncio.run(
            TaskHandler(db).add_task(
                owner_id,
                "plan:2026-05-01 cat:工作 p:2",
                SimpleNamespace(),
            )
        )

        assert result["status"] == "error"
        assert "标题不能为空" in result["message"]
        assert db.get_items(owner_id, filters={"type": "task"}) == []
    finally:
        db.cleanup()


@pytest.mark.parametrize(
    "filter_str",
    [
        "open done",
        "all page:1",
        "cat:工作 cat:家庭",
        "#工作 #家庭",
        "p:1 p:2",
        "today overdue",
        'cat:"未闭合',
    ],
)
def test_task_list_rejects_duplicate_or_conflicting_controls(filter_str: str) -> None:
    result = asyncio.run(
        TaskHandler(SimpleNamespace()).list_tasks(
            "task-list-validation",
            filter_str,
            SimpleNamespace(),
        )
    )
    assert result["status"] == "error"


def test_task_list_uses_exact_total_and_rejects_out_of_range_page(tmp_path: Path):
    from plugins.pendo.config import PendoConfig

    db = Database(str(tmp_path / "pendo-task-exact-list.db"))
    owner_id = "task-exact-list"
    total = PendoConfig.LIST_PAGE_SIZE * 2 + 1
    try:
        for index in range(total):
            db.insert_item(
                {
                    "id": f"exact-task-{index:02d}",
                    "owner_id": owner_id,
                    "type": "task",
                    "title": f"精确待办 {index:02d}",
                    "category": "工作" if index % 2 else "家庭",
                    "priority": 3,
                    "status": "open",
                    "created_at": f"2026-05-01T09:{index:02d}:00",
                    "updated_at": f"2026-05-01T09:{index:02d}:00",
                }
            )

        handler = TaskHandler(db)
        first_page = asyncio.run(handler.list_tasks(owner_id, "open", SimpleNamespace()))
        last_page = asyncio.run(handler.list_tasks(owner_id, "open page:3", SimpleNamespace()))
        past_end = asyncio.run(handler.list_tasks(owner_id, "open page:4", SimpleNamespace()))

        assert first_page["status"] == "success"
        assert f"共{total}项" in first_page["message"]
        assert last_page["status"] == "success"
        assert "第3页" in last_page["message"]
        assert past_end == {"status": "error", "message": "❌ 第 4 页超出范围"}
    finally:
        db.cleanup()


def test_task_time_shortcuts_accept_offset_aware_legacy_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db = Database(str(tmp_path / "pendo-task-aware-deadline.db"))
    owner_id = "task-aware-deadline"
    try:
        db.insert_item(
            {
                "id": "task-aware-future",
                "owner_id": owner_id,
                "type": "task",
                "title": "跨时区截止",
                "category": "工作",
                "deadline_at": "2032-01-02T09:00:00+08:00",
                "priority": 3,
                "status": "open",
                "created_at": "2032-01-01T09:00:00",
                "updated_at": "2032-01-01T09:00:00",
            }
        )
        handler = TaskHandler(db)
        monkeypatch.setattr(handler, "_user_local_now", lambda _user_id: datetime(2032, 1, 1, 8))

        result = asyncio.run(handler.list_tasks(owner_id, "upcoming", SimpleNamespace()))

        assert result["status"] == "success"
        assert "task-aware-future" in result["message"]
    finally:
        db.cleanup()


def test_task_edit_can_clear_schedule_and_skips_identical_second_write(tmp_path: Path):
    db = Database(str(tmp_path / "pendo-task-clear-noop.db"))
    owner_id = "task-clear-noop"
    try:
        db.insert_item(
            {
                "id": "task-clear-fields",
                "owner_id": owner_id,
                "type": "task",
                "title": "提交材料",
                "category": "工作",
                "plan_date": "2026-05-01",
                "deadline_at": "2026-05-02T10:00:00",
                "remind_times": ["2026-05-02T09:00:00"],
                "priority": 2,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        handler = TaskHandler(db)

        first = asyncio.run(
            handler.edit_task(
                owner_id,
                "task-clear-fields plan:none deadline:clear remind:unset",
                SimpleNamespace(),
            )
        )
        second = asyncio.run(
            handler.edit_task(
                owner_id,
                "task-clear-fields plan:none deadline:clear remind:unset",
                SimpleNamespace(),
            )
        )
        task = db.get_item("task-clear-fields", owner_id)
        edit_logs = (
            db.get_connection()
            .execute(
                "SELECT COUNT(*) FROM operation_logs WHERE user_id = ? AND action = 'edit_task'",
                (owner_id,),
            )
            .fetchone()[0]
        )

        assert first["status"] == "success"
        assert second == {"status": "warning", "message": "⚠️ 待办内容没有变化"}
        assert task.title == "提交材料"
        assert task.plan_date is None
        assert task.deadline_at is None
        assert task.remind_times == []
        assert edit_logs == 1
    finally:
        db.cleanup()


def test_task_status_transitions_are_idempotent_and_require_one_id(tmp_path: Path):
    db = Database(str(tmp_path / "pendo-task-status-idempotent.db"))
    owner_id = "task-status-idempotent"
    try:
        db.insert_item(
            {
                "id": "task-status-one",
                "owner_id": owner_id,
                "type": "task",
                "title": "完成审查",
                "category": "工作",
                "priority": 2,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        handler = TaskHandler(db)

        already_open = asyncio.run(
            handler.mark_undone(owner_id, "task-status-one", SimpleNamespace())
        )
        first_done = asyncio.run(handler.mark_done(owner_id, "task-status-one", SimpleNamespace()))
        completed_at = db.get_item("task-status-one", owner_id).completed_at
        second_done = asyncio.run(handler.mark_done(owner_id, "task-status-one", SimpleNamespace()))
        extra_id = asyncio.run(
            handler.mark_done(owner_id, "task-status-one extra", SimpleNamespace())
        )
        complete_logs = (
            db.get_connection()
            .execute(
                "SELECT COUNT(*) FROM operation_logs WHERE user_id = ? AND action = 'complete_task'",
                (owner_id,),
            )
            .fetchone()[0]
        )
        reopen_logs = (
            db.get_connection()
            .execute(
                "SELECT COUNT(*) FROM operation_logs WHERE user_id = ? AND action = 'reopen_task'",
                (owner_id,),
            )
            .fetchone()[0]
        )

        assert already_open["status"] == "success"
        assert "已是未完成状态" in already_open["message"]
        assert first_done["status"] == "success"
        assert second_done["status"] == "success"
        assert "已是完成状态" in second_done["message"]
        assert extra_id["status"] == "error"
        assert db.get_item("task-status-one", owner_id).completed_at == completed_at
        assert complete_logs == 1
        assert reopen_logs == 0
    finally:
        db.cleanup()


def test_task_category_delete_requires_one_exact_target(tmp_path: Path):
    db = Database(str(tmp_path / "pendo-task-delete-exact.db"))
    owner_id = "task-delete-exact"
    try:
        db.insert_item(
            {
                "id": "task-delete-one",
                "owner_id": owner_id,
                "type": "task",
                "title": "整理资料",
                "category": "工作 项目",
                "priority": 3,
                "status": "open",
                "created_at": "2026-05-01T09:00:00",
                "updated_at": "2026-05-01T09:00:00",
            }
        )
        handler = TaskHandler(db)

        rejected = asyncio.run(
            handler.delete_task(owner_id, 'cat:"工作 项目" extra', SimpleNamespace())
        )
        assert rejected["status"] == "error"
        assert db.get_item("task-delete-one", owner_id) is not None

        deleted = asyncio.run(handler.delete_task(owner_id, 'cat:"工作 项目"', SimpleNamespace()))
        assert deleted["status"] == "success"
        assert db.get_item("task-delete-one", owner_id) is None
    finally:
        db.cleanup()
