"""Pendo 用户命令的同一对象生命周期与字段级业务回归。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from plugins.pendo.handlers.diary import DiaryHandler
from plugins.pendo.handlers.event import EventHandler
from plugins.pendo.handlers.ledger import LedgerHandler
from plugins.pendo.handlers.note import NoteHandler
from plugins.pendo.handlers.task import TaskHandler
from plugins.pendo.models.item import TaskStatus
from plugins.pendo.utils.identifiers import is_canonical_internal_id


class _EventParser:
    """为命令生命周期提供确定性的日程编辑与提醒规则。"""

    async def parse_event_with_ai(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def parse_natural_language(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def build_remind_times_from_offsets(
        self, start_time: str, _offsets: list[str], **_kwargs: Any
    ) -> list[str]:
        return [start_time]

    def build_reminder_rules_from_description(self, description: str) -> list[dict[str, int]]:
        if "2小时" in description:
            return [{"offset_seconds": 7200}, {"offset_seconds": 0}]
        if "30分钟" in description:
            return [{"offset_seconds": 1800}, {"offset_seconds": 0}]
        return [{"offset_seconds": 0}]


class _NoEventConflict:
    def detect_conflict(
        self, _user_id: str, _start_time: str, _end_time: str | None = None
    ) -> list[Any]:
        return []


def _task_status_value(value: Any) -> str:
    return value.value if isinstance(value, TaskStatus) else str(value)


def _snapshot(item: Any) -> dict[str, Any]:
    return deepcopy(item.to_dict())


@pytest.mark.asyncio
async def test_todo_same_id_covers_all_fields_statuses_invalid_input_and_delete(db) -> None:
    """一条待办必须用同一 ID 完成全字段增查改、状态迁移和删除。"""

    owner   = "lifecycle-todo"
    handler = TaskHandler(db)
    created = await handler.handle(
        owner,
        'add "初始待办" plan:2035-01-10 deadline:2035-01-10T18:00 '
        'remind:"2035-01-10T17:00,2035-01-10T17:30" cat:"深度工作" p:2 #初始',
        {},
        group_id=71001,
    )

    assert created["status"] == "success"
    task_id = created["item_id"]
    task    = db.get_item(task_id, owner)
    assert task is not None
    assert task.title == "初始待办"
    assert task.plan_date == "2035-01-10"
    assert task.deadline_at == "2035-01-10T10:00:00+00:00"
    assert task.remind_times == [
        "2035-01-10T09:00:00+00:00",
        "2035-01-10T09:30:00+00:00",
    ]
    assert task.category == "深度工作"
    assert task.priority == 2
    assert task.tags == ["初始"]
    assert task.context == {"group_id": 71001}
    assert _task_status_value(task.status) == "open"

    viewed = await handler.handle(owner, f"view {task_id}", {})
    assert viewed["status"] == "success"
    assert task_id[:8] in viewed["message"]
    assert task_id not in viewed["message"]
    assert "初始待办" in viewed["message"]

    before_invalid = _snapshot(task)
    invalid        = await handler.handle(owner, f"edit {task_id} plan:not-a-date", {})
    assert invalid["status"] == "error"
    assert "参数无效" in invalid["message"]
    assert "YYYY-MM-DD" in invalid["message"]
    assert "XQ-PLUGIN-UNEXPECTED" not in invalid["message"]
    assert _snapshot(db.get_item(task_id, owner)) == before_invalid

    edited = await handler.handle(
        owner,
        f'edit {task_id} "修改后待办" plan:2035-02-11 deadline:2035-02-11T19:30 '
        'remind:"2035-02-11T18:00" cat:"发布" p:1 #已改',
        {},
    )
    assert edited["status"] == "success"
    task = db.get_item(task_id, owner)
    assert task.title == "修改后待办"
    assert task.plan_date == "2035-02-11"
    assert task.deadline_at == "2035-02-11T11:30:00+00:00"
    assert task.remind_times == ["2035-02-11T10:00:00+00:00"]
    assert task.category == "发布"
    assert task.priority == 1
    assert task.tags == ["已改"]

    for command, expected in (
        ("done", "done"),
        ("done", "done"),
        ("undone", "open"),
        ("cancel", "cancelled"),
        ("undone", "open"),
    ):
        result = await handler.handle(owner, f"{command} {task_id}", {})
        assert result["status"] == "success"
        assert _task_status_value(db.get_item(task_id, owner).status) == expected

    deleted = await handler.handle(owner, f"delete {task_id}", {})
    assert deleted["status"] == "success"
    assert db.get_item(task_id, owner) is None
    missing = await handler.handle(owner, f"view {task_id}", {})
    assert missing["status"] == "error"


@pytest.mark.asyncio
async def test_note_same_id_covers_fields_mutations_links_invalid_input_and_delete(db) -> None:
    """笔记编辑、追加、标签和关联必须落在创建返回的同一 ID 上。"""

    owner         = "lifecycle-note"
    handler       = NoteHandler(db)
    first_target  = await handler.handle(owner, "add 关联目标一 cat:资料 #目标", {})
    second_target = await handler.handle(owner, "add 关联目标二 cat:资料 #目标", {})
    note          = await handler.handle(
        owner,
        f'add title:"初始标题" content 初始正文 cat:"研究" #初始 ref:{first_target["item_id"]}',
        {},
        group_id=71002,
    )

    assert note["status"] == "success"
    note_id = note["item_id"]
    stored  = db.get_item(note_id, owner)
    assert stored.title == "初始标题"
    assert stored.content == "初始正文"
    assert stored.category == "研究"
    assert stored.tags == ["初始"]
    assert stored.related_items == [first_target["item_id"]]
    assert stored.context == {"group_id": 71002}

    viewed = await handler.handle(owner, f"view {note_id}", {})
    assert viewed["status"] == "success"
    assert "初始正文" in viewed["message"]

    before_invalid = _snapshot(db.get_item(note_id, owner))
    invalid        = await handler.handle(owner, f"link {note_id} {note_id}", {})
    assert invalid["status"] == "error"
    assert _snapshot(db.get_item(note_id, owner)) == before_invalid

    edited = await handler.handle(
        owner,
        f'edit {note_id} title:"修改标题" content 修改正文 cat:"成果" #编辑 '
        f"ref:{second_target['item_id']}",
        {},
    )
    assert edited["status"] == "success"
    appended = await handler.handle(owner, f"append {note_id} 追加结论", {})
    tagged   = await handler.handle(owner, f"tag {note_id} #复盘 #共享", {})
    untagged = await handler.handle(owner, f"untag {note_id} #共享", {})
    linked   = await handler.handle(owner, f"link {note_id} {first_target['item_id']}", {})
    assert [item["status"] for item in (appended, tagged, untagged)] == [
        "success",
        "success",
        "success",
    ]
    assert linked["status"] == "info", "创建时已有的关联必须幂等而不是重复写入"

    stored = db.get_item(note_id, owner)
    assert stored.title == "修改标题"
    assert stored.content == "修改正文\n\n追加结论"
    assert stored.category == "成果"
    assert stored.tags == ["编辑", "复盘"]
    assert stored.related_items == [first_target["item_id"], second_target["item_id"]]
    assert len(stored.references) == 2

    deleted = await handler.handle(owner, f"delete {note_id}", {})
    assert deleted["status"] == "success"
    assert db.get_item(note_id, owner) is None
    for target_id in (first_target["item_id"], second_target["item_id"]):
        assert (await handler.handle(owner, f"delete {target_id}", {}))["status"] == "success"


@pytest.mark.asyncio
async def test_diary_same_id_covers_all_command_fields_and_rejects_invalid_boolean(db) -> None:
    """日记支持的全部显式字段都要持久化，非法收藏值不能创建记录。"""

    owner   = "lifecycle-diary"
    handler = DiaryHandler(db)
    created = await handler.handle(
        owner,
        'add 2035-03-12 完整日记 weather:"多云转晴" location:"上海 徐汇" '
        "mood:happy score:8 tags:工作,复盘 favorite:true",
        {},
        group_id=71003,
    )

    assert created["status"] == "success"
    diary_id = created["item_id"]
    diary    = db.get_item(diary_id, owner)
    assert diary.content == "完整日记"
    assert diary.diary_date == "2035-03-12"
    assert (
        datetime.fromisoformat(diary.entry_time)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
        == "2035-03-12"
    )
    assert diary.weather == "多云转晴"
    assert diary.location == "上海 徐汇"
    assert diary.mood == "happy"
    assert diary.mood_score == 8
    assert diary.tags == ["工作", "复盘"]
    assert diary.is_favorite is True
    assert diary.context == {"group_id": 71003}

    count_before = len(db.get_items(owner, filters={"type": "diary"}))
    invalid = await handler.handle(owner, "add 不应创建 favorite:maybe", {})
    assert invalid["status"] == "error"
    assert "favorite" in invalid["message"]
    assert len(db.get_items(owner, filters={"type": "diary"})) == count_before

    unsupported_edit = await handler.handle(owner, f"edit {diary_id} 任意内容", {})
    assert unsupported_edit["status"] == "error"
    assert db.get_item(diary_id, owner).content == "完整日记"

    for query in (diary_id, "2035-03-12"):
        viewed = await handler.handle(owner, f"view {query}", {})
        assert viewed["status"] == "success"
        assert "完整日记" in viewed["message"]
    listed = await handler.handle(owner, "list 2035-03 mood:happy #工作", {})
    assert listed["status"] == "success"
    assert diary_id[:8] in listed["message"]
    assert diary_id not in listed["message"]

    deleted = await handler.handle(owner, f"delete {diary_id}", {})
    assert deleted["status"] == "success"
    assert db.get_item(diary_id, owner) is None


@pytest.mark.asyncio
async def test_ledger_same_id_covers_every_edit_field_and_all_transaction_types(db) -> None:
    """一条账目在同一 ID 上覆盖支出、转账、收入及全部可编辑字段。"""

    owner   = "lifecycle-ledger"
    handler = LedgerHandler(db)
    created = await handler.handle(
        owner,
        'add 35.50 "初始支出" type:expense cat:餐饮 account:微信 '
        'merchant:"一号食堂" date:2035-04-02 remark:"午餐" currency:CNY',
        {},
        group_id=71004,
    )

    assert created["status"] == "success"
    ledger_id = created["item_id"]
    item      = db.get_item(ledger_id, owner)
    assert item.amount_cents == 3550
    assert item.transaction_type == "expense"
    assert item.ledger_category == "餐饮"
    assert item.account_name == "微信"
    assert item.counter_account_name == ""
    assert item.merchant == "一号食堂"
    assert item.ledger_date == "2035-04-02"
    assert item.remark == "午餐"
    assert item.currency == "CNY"
    assert item.context == {"group_id": 71004}

    before_invalid = _snapshot(item)
    invalid        = await handler.handle(owner, f"edit {ledger_id} to:银行卡", {})
    assert invalid["status"] == "error"
    assert _snapshot(db.get_item(ledger_id, owner)) == before_invalid

    to_transfer = await handler.handle(
        owner,
        f'edit {ledger_id} amount:99.99 title:"资金调拨" cat:转账 type:transfer '
        'account:微信 to:银行卡 merchant:"本人" date:2035-04-03 '
        'remark:"全字段修改" currency:CNY',
        {},
    )
    assert to_transfer["status"] == "success"
    item = db.get_item(ledger_id, owner)
    assert item.amount_cents == 9999
    assert item.title == "资金调拨"
    assert item.transaction_type == "transfer"
    assert item.ledger_category == "转账"
    assert item.account_name == "微信"
    assert item.counter_account_name == "银行卡"
    assert item.merchant == "本人"
    assert item.ledger_date == "2035-04-03"
    assert item.remark == "全字段修改"
    assert item.currency == "CNY"

    to_income = await handler.handle(
        owner,
        f"edit {ledger_id} type:income cat:工资 account:银行卡 merchant:单位",
        {},
    )
    assert to_income["status"] == "success"
    item = db.get_item(ledger_id, owner)
    assert item.transaction_type == "income"
    assert item.ledger_category == "工资"
    assert item.counter_account_name == ""

    viewed = await handler.handle(owner, f"view {ledger_id}", {})
    assert viewed["status"] == "success"
    assert "99.99" in viewed["message"]
    listed = await handler.handle(owner, "list 2035-04 type:income account:银行卡", {})
    assert listed["status"] == "success"
    assert ledger_id[:8] in listed["message"]
    assert ledger_id not in listed["message"]
    summary = await handler.handle(owner, "summary 2035-04", {})
    assert summary["status"] == "success"

    deleted = await handler.handle(owner, f"delete {ledger_id}", {})
    assert deleted["status"] == "success"
    assert db.get_item(ledger_id, owner) is None


def _event_handler(db) -> EventHandler:
    return EventHandler(db, _EventParser(), _NoEventConflict())


async def _set_event_updates(handler: EventHandler, updates: dict[str, Any]) -> None:
    async def parse_updates(_changes: str, _current: Any) -> dict[str, Any]:
        return deepcopy(updates)

    handler._parse_updates = parse_updates  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_single_event_same_id_covers_fields_reminders_invalid_edit_and_delete(db) -> None:
    """单次 Event 在同一 ID 上覆盖可用字段、提醒、错误不变性和删除。"""

    owner   = "lifecycle-event-single"
    handler = _event_handler(db)
    created = await handler.create_event(
        owner,
        {
            "title": "单次会议",
            "content": "初始议程",
            "category": "工作",
            "start_time": "2035-05-01T09:00:00",
            "end_time": "2035-05-01T10:30:00",
            "location": "一号会议室",
            "tags": ["项目", "会议"],
            "notes": "带电脑",
            "context": {"group_id": 71005},
            "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
        },
        {},
    )
    assert created["status"] == "success"
    event_id = created["item_id"]
    event    = db.get_item(event_id, owner)
    assert event.event_role == "single"
    assert event.title == "单次会议"
    assert event.content == "初始议程"
    assert event.category == "工作"
    assert event.start_time == "2035-05-01T01:00:00+00:00"
    assert event.end_time == "2035-05-01T02:30:00+00:00"
    assert event.location == "一号会议室"
    assert event.tags == ["项目", "会议"]
    assert event.notes == "带电脑"
    assert event.context == {"group_id": 71005}
    assert event.remind_times == [
        "2035-05-01T00:00:00+00:00",
        "2035-05-01T01:00:00+00:00",
    ]

    before_invalid = _snapshot(event)
    invalid        = await handler.edit_event(owner, event_id, {})
    assert invalid["status"] == "error"
    assert _snapshot(db.get_item(event_id, owner)) == before_invalid

    await _set_event_updates(
        handler,
        {
            "title": "单次会议已改",
            "content": "新议程",
            "category": "发布",
            "start_time": "2035-05-02T11:00:00",
            "end_time": "2035-05-02T12:00:00",
            "location": "二号会议室",
            "tags": ["已改"],
            "notes": "线上同步",
        },
    )
    edited = await handler.edit_event(owner, f"{event_id} 修改全部字段", {})
    assert edited["status"] == "success"
    event = db.get_item(event_id, owner)
    assert event.title == "单次会议已改"
    assert event.content == "新议程"
    assert event.category == "发布"
    assert event.start_time == "2035-05-02T03:00:00+00:00"
    assert event.end_time == "2035-05-02T04:00:00+00:00"
    assert event.location == "二号会议室"
    assert event.tags == ["已改"]
    assert event.notes == "线上同步"
    assert event.remind_times == [
        "2035-05-02T02:00:00+00:00",
        "2035-05-02T03:00:00+00:00",
    ]

    reminders = await handler.set_reminders(owner, f"{event_id} 提前2小时提醒", {})
    assert reminders["status"] == "success"
    assert db.get_item(event_id, owner).remind_times == [
        "2035-05-02T01:00:00+00:00",
        "2035-05-02T03:00:00+00:00",
    ]
    viewed = await handler.view_event(owner, event_id, {})
    assert viewed["status"] == "success"
    for expected in (
        "单次会议已改",
        "2035-05-02 11:00",
        "2035-05-02 12:00",
        "分类: 发布",
        "内容: 新议程",
        "二号会议室",
        "线上同步",
        "已改",
    ):
        assert expected in viewed["message"]

    deleted = await handler.delete_event(owner, event_id, {})
    assert deleted["status"] == "success"
    assert db.get_item(event_id, owner) is None
    assert (await handler.view_event(owner, event_id, {}))["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload", "child_suffixes"),
    [
        (
            "recurring",
            {
                "title": "重复晨会",
                "content": "每日同步",
                "category": "工作",
                "start_time": "2035-06-01T09:00:00",
                "end_time": "2035-06-01T09:30:00",
                "location": "线上",
                "tags": ["重复"],
                "notes": "三次",
                "context": {"group_id": 71006},
                "rrule": "FREQ=DAILY;COUNT=3",
                "reminder_rules": [{"offset_seconds": 1800}, {"offset_seconds": 0}],
            },
            ("20350601", "20350602", "20350603"),
        ),
        (
            "multi_node",
            {
                "title": "发布流程",
                "content": "完整发布",
                "category": "工作",
                "location": "总部",
                "tags": ["多节点"],
                "notes": "按顺序执行",
                "context": {"group_id": 71007},
                "milestones": [
                    {"name": "提审", "time": "2035-07-01T09:00:00", "notes": "先提审"},
                    {"name": "灰度", "time": "2035-07-02T12:00:00", "notes": "观察"},
                    {"name": "全量", "time": "2035-07-03T18:00:00", "notes": "收尾"},
                ],
                "reminder_rules": [{"offset_seconds": 3600}, {"offset_seconds": 0}],
            },
            ("m01", "m02", "m03"),
        ),
    ],
)
async def test_event_collection_types_cover_collection_and_child_crud(
    db, kind: str, payload: dict[str, Any], child_suffixes: tuple[str, ...]
) -> None:
    """重复和多节点 Event 都要验证集合 ID 与同一组子 ID 的完整 CRUD。"""

    owner   = f"lifecycle-event-{kind}"
    handler = _event_handler(db)
    created = await handler.create_event(owner, deepcopy(payload), {}, allow_conflict=True)
    assert created["status"] == "success"
    collection_id = created["item_id"]
    collection    = db.get_event_collection(collection_id, owner)
    assert collection is not None
    assert collection["kind"] == kind
    for field in ("title", "content", "category", "location", "tags", "notes", "context"):
        assert collection[field] == payload[field]

    children  = db.get_collection_events(collection_id, owner)
    child_ids = tuple(child.id for child in children)
    assert len(set(child_ids)) == 3
    assert all(is_canonical_internal_id(child_id) for child_id in child_ids)
    assert [child.event_node_key for child in children] == list(child_suffixes)
    assert len(children) == 3
    assert [child.event_index for child in children] == [1, 2, 3]
    assert all(child.event_collection_id == collection_id for child in children)
    assert all(child.event_collection_kind == kind for child in children)
    assert all(child.context == payload["context"] for child in children)
    assert all(child.remind_times for child in children)
    if kind == "recurring":
        assert all(child.event_role == "recurring_occurrence" for child in children)
        assert all(child.content == payload["content"] for child in children)
        assert all(child.notes == payload["notes"] for child in children)
    else:
        assert all(child.event_role == "multi_node_child" for child in children)
        assert [child.title for child in children] == ["提审", "灰度", "全量"]
        assert [child.notes for child in children] == ["先提审", "观察", "收尾"]

    collection_view = await handler.view_event(owner, collection_id, {})
    child_view      = await handler.view_event(owner, child_ids[1], {})
    assert collection_view["status"] == child_view["status"] == "success"
    assert collection_id[:8] in child_view["message"]
    assert collection_id not in child_view["message"]
    for expected in (
        payload["content"],
        f"分类: {payload['category']}",
        payload["location"],
        payload["notes"],
        payload["tags"][0],
        "2035-",
    ):
        assert expected in collection_view["message"]
    for expected in (f"分类: {payload['category']}", payload["location"], "2035-"):
        assert expected in child_view["message"]
    if kind == "recurring":
        assert payload["content"] in child_view["message"]

    collection_before = deepcopy(collection)
    invalid           = await handler.edit_event(owner, f"{collection_id} 时间改到2035-08-01", {})
    assert invalid["status"] == "warning"
    assert db.get_event_collection(collection_id, owner) == collection_before

    await _set_event_updates(
        handler,
        {
            "title": "集合标题已改",
            "content": "集合内容已改",
            "category": "发布",
            "location": "新地点",
            "tags": ["集合标签"],
            "notes": "集合备注已改",
            "start_time": "2035-12-31T23:59:00",
        },
    )
    collection_edit = await handler.edit_event(owner, f"{collection_id} 修改集合", {})
    assert collection_edit["status"] == "success"
    collection = db.get_event_collection(collection_id, owner)
    assert {
        field: collection[field]
        for field in ("title", "content", "category", "location", "tags", "notes")
    } == {
        "title": "集合标题已改",
        "content": "集合内容已改",
        "category": "发布",
        "location": "新地点",
        "tags": ["集合标签"],
        "notes": "集合备注已改",
    }
    assert collection["start_time"] == collection_before["start_time"], (
        "集合编辑不得把单节点时间误写到集合时间"
    )

    leaf_before = db.get_item(child_ids[1], owner)
    await _set_event_updates(
        handler,
        {
            "title": "中间节点已改",
            "content": "节点内容",
            "category": "节点分类",
            "location": "节点地点",
            "tags": ["节点标签"],
            "notes": "节点备注",
            "start_time": "2035-08-02T15:00:00",
            "end_time": "2035-08-02T16:00:00",
        },
    )
    leaf_edit = await handler.edit_event(owner, f"{child_ids[1]} 修改节点", {})
    assert leaf_edit["status"] == "success"
    leaf = db.get_item(child_ids[1], owner)
    assert leaf.id == leaf_before.id
    assert leaf.title == "中间节点已改"
    assert leaf.content == "节点内容"
    assert leaf.category == "节点分类"
    assert leaf.location == "节点地点"
    assert leaf.tags == ["节点标签"]
    assert leaf.notes == "节点备注"
    assert leaf.start_time == "2035-08-02T07:00:00+00:00"
    assert leaf.end_time == "2035-08-02T08:00:00+00:00"

    reminder_result = await handler.set_reminders(owner, f"{child_ids[1]} 提前30分钟提醒", {})
    assert reminder_result["status"] == "success"
    assert db.get_item(child_ids[1], owner).remind_times == [
        "2035-08-02T06:30:00+00:00",
        "2035-08-02T07:00:00+00:00",
    ]

    delete_leaf = await handler.delete_event(owner, child_ids[1], {})
    assert delete_leaf["status"] == "success"
    assert db.get_item(child_ids[1], owner) is None
    assert db.get_event_collection(collection_id, owner) is not None
    assert {child.id for child in db.get_collection_events(collection_id, owner)} == {
        child_ids[0],
        child_ids[2],
    }

    delete_collection = await handler.delete_event(owner, collection_id, {})
    assert delete_collection["status"] == "success"
    assert db.get_event_collection(collection_id, owner) is None
    assert db.get_item(child_ids[0], owner) is None
    assert db.get_item(child_ids[2], owner) is None
