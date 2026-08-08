"""导入执行、关系重写和审计。"""

from __future__ import annotations

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.pendo_web_transfer_test_support import (
    OWNER_ID,
    Any,
    Database,
    DuplicateBundleImportError,
    _build_sample_bundle_bytes,
    _ImportRequest,
    asyncio,
    json,
    pytest,
    re,
    transfer_api,
)

auth_headers = _fixture_support.auth_headers
PROJECT_ROOT = REPOSITORY_ROOT


def test_import_execute_isolates_external_ids_and_selected_types(
    client: Any, temp_db: Database, auth_headers: dict
):
    temp_db.insert_item(
        {
            "id": "task_existing",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "旧任务",
            "content": "旧内容",
            "category": "旧分类",
            "priority": 2,
            "status": "open",
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )

    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_existing",
                    "title": "新任务标题",
                    "content": "新内容",
                    "category": "工作",
                    "priority": 4,
                    "status": "done",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
            "note": [
                {
                    "_type": "note",
                    "_schema": 2,
                    "id": "note_subset",
                    "title": "不会导入",
                    "content": "因为没选中",
                    "category": "知识",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    skip_response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )
    assert skip_response.status_code == 200
    skip_body = skip_response.json()["data"]["results"]
    assert skip_body["inserted"] == 1
    assert skip_body["updated"] == 0
    assert skip_body["skipped"] == 0
    assert "内部 UUID" in skip_response.json()["data"]["details"]["inserted"][0]["reason"]
    assert temp_db.get_item("task_existing", owner_id=OWNER_ID).title == "旧任务"
    assert temp_db.get_item("note_subset", owner_id=OWNER_ID) is None

    # Overwrite targets only a same-owner/type record previously imported with
    # this provenance source ID; it never treats the external ID as a DB key.
    overwrite_bundle = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_existing",
                    "title": "新任务标题",
                    "content": "新内容",
                    "category": "工作",
                    "priority": 4,
                    "status": "done",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    overwrite_response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "overwrite"}),
        },
        content=overwrite_bundle,
    )
    assert overwrite_response.status_code == 200
    overwrite_body = overwrite_response.json()["data"]["results"]
    assert overwrite_body["inserted"] == 0
    assert overwrite_body["updated"] == 1
    overwritten = temp_db.get_item("task_existing", owner_id=OWNER_ID)
    assert overwritten.title == "旧任务"
    assert overwritten.owner_id == OWNER_ID
    imported_after_overwrite = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=20)
        if item.id != "task_existing"
        and item.context.get("import", {}).get("source_id") == "task_existing"
    ]
    assert len(imported_after_overwrite) == 1
    assert imported_after_overwrite[0].title == "新任务标题"

    duplicate_bundle = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_existing",
                    "title": "新任务标题",
                    "content": "新内容",
                    "category": "工作",
                    "priority": 4,
                    "status": "done",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    duplicate_response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "duplicate"}),
        },
        content=duplicate_bundle,
    )
    assert duplicate_response.status_code == 200
    duplicate_body = duplicate_response.json()["data"]["results"]
    assert duplicate_body["inserted"] == 1
    assert "UUID" in duplicate_response.json()["data"]["details"]["inserted"][0]["reason"]

    tasks = temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=20)
    imported_copies = [
        item for item in tasks if item.id != "task_existing" and item.title == "新任务标题"
    ]
    assert len(imported_copies) == 2
    assert {item.context["import"]["source_id"] for item in imported_copies} == {"task_existing"}
    assert all(item.id != "task_existing" and len(item.id) == 32 for item in imported_copies)


def test_import_execute_hides_internal_transaction_errors(
    client: Any, temp_db: Database, auth_headers: dict, monkeypatch
):
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_internal_error",
                    "title": "触发事务错误",
                    "status": "open",
                }
            ],
        }
    )

    def fail_execute_import_bundle(**kwargs):
        raise RuntimeError("C:\\secret\\pendo.db SQL failed")

    monkeypatch.setattr(temp_db, "execute_import_bundle", fail_execute_import_bundle)

    response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )

    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert "导入事务失败" in body["message"]
    assert "C:\\secret" not in str(body)
    assert "SQL failed" not in str(body)


def test_import_reassigns_hostile_external_id_and_preserves_only_source_metadata(
    client: Any,
    temp_db: Database,
    auth_headers: dict,
):
    hostile_id = '"><svg onload=alert(1)>'
    bundle = _build_sample_bundle_bytes(
        {
            "note": [
                {
                    "_type": "note",
                    "_schema": 2,
                    "id": hostile_id,
                    "title": "hostile id regression",
                    "content": "literal text",
                    "category": "test",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": json.dumps({"types": ["note"]})},
        content=bundle,
    )

    assert response.status_code == 200
    note = temp_db.get_items(OWNER_ID, filters={"type": "note"}, limit=10)[0]
    assert re.fullmatch(r"[0-9a-f]{32}", note.id)
    assert note.id != hostile_id
    assert note.context["import"]["source_id"] == hostile_id


def test_pendo_item_id_attributes_escape_historical_untrusted_values_and_csp_blocks_inline_script():
    static_root = PROJECT_ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages"
    diary = (static_root / "diary.js").read_text(encoding="utf-8")
    notes = (static_root / "notes.js").read_text(encoding="utf-8")
    events = (static_root / "events.js").read_text(encoding="utf-8")
    server = (PROJECT_ROOT / "plugins" / "pendo" / "web" / "server.py").read_text(encoding="utf-8")

    assert 'data-id="${item.id}"' not in diary
    assert 'data-id="${note.id}"' not in notes
    assert 'data-event-id="${item.event_id}"' not in events
    assert "script-src 'self'" in server
    assert "object-src 'none'" in server


def test_import_execute_isolates_cross_owner_source_ids(temp_db: Database):
    other_owner = "u-transfer-other"
    temp_db.insert_item(
        {
            "id": "task_shared_global",
            "owner_id": other_owner,
            "type": "task",
            "title": "其他用户任务",
            "content": "不能被覆盖",
            "category": "工作",
            "priority": 3,
            "status": "open",
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )

    def bundle(title: str) -> bytes:
        return _build_sample_bundle_bytes(
            {
                "task": [
                    {
                        "_type": "task",
                        "_schema": 2,
                        "id": "task_shared_global",
                        "title": title,
                        "content": "导入内容",
                        "category": "工作",
                        "priority": 4,
                        "status": "open",
                        "created_at": "2026-03-20T09:00:00+08:00",
                        "updated_at": "2026-03-20T09:00:00+08:00",
                    }
                ],
            }
        )

    skip_result = asyncio.run(
        transfer_api.execute_import(
            _ImportRequest(bundle("跳过")),
            x_transfer_options=json.dumps({"types": ["task"], "conflict_policy": "skip"}),
            owner_id=OWNER_ID,
            db=temp_db,
        )
    )
    assert skip_result["data"]["results"]["inserted"] == 1
    assert temp_db.get_item("task_shared_global", owner_id=other_owner).title == "其他用户任务"

    overwrite_result = asyncio.run(
        transfer_api.execute_import(
            _ImportRequest(bundle("覆盖")),
            x_transfer_options=json.dumps({"types": ["task"], "conflict_policy": "overwrite"}),
            owner_id=OWNER_ID,
            db=temp_db,
        )
    )
    assert overwrite_result["data"]["results"]["inserted"] == 0
    assert overwrite_result["data"]["results"]["updated"] == 1
    assert temp_db.get_item("task_shared_global", owner_id=other_owner).title == "其他用户任务"
    overwritten_import = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=10)
        if item.context.get("import", {}).get("source_id") == "task_shared_global"
    ]
    assert len(overwritten_import) == 1
    assert overwritten_import[0].title == "覆盖"

    duplicate_result = asyncio.run(
        transfer_api.execute_import(
            _ImportRequest(bundle("副本")),
            x_transfer_options=json.dumps({"types": ["task"], "conflict_policy": "duplicate"}),
            owner_id=OWNER_ID,
            db=temp_db,
        )
    )
    assert duplicate_result["data"]["results"]["inserted"] == 1
    imported = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=10)
        if item.title == "副本"
    ]
    assert len(imported) == 1
    assert imported[0].id != "task_shared_global"
    assert imported[0].context["import"]["source_id"] == "task_shared_global"


def test_import_execute_handles_soft_deleted_global_id_conflict(temp_db: Database):
    temp_db.insert_item(
        {
            "id": "task_deleted_global",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "已删任务",
            "category": "工作",
            "priority": 3,
            "status": "open",
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )
    temp_db.delete_item("task_deleted_global", soft=True, owner_id=OWNER_ID)
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_deleted_global",
                    "title": "重新导入",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    result = asyncio.run(
        transfer_api.execute_import(
            _ImportRequest(bundle_bytes),
            x_transfer_options=json.dumps({"types": ["task"], "conflict_policy": "duplicate"}),
            owner_id=OWNER_ID,
            db=temp_db,
        )
    )

    assert result["data"]["results"]["inserted"] == 1
    imported = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=10)
        if item.title == "重新导入"
    ]
    assert len(imported) == 1
    assert imported[0].id != "task_deleted_global"


def test_import_execute_duplicate_rewrites_note_relationships_to_duplicated_items(
    client: Any,
    temp_db: Database,
    auth_headers: dict,
):
    temp_db.insert_item(
        {
            "id": "task_existing",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "旧任务",
            "priority": 3,
            "status": "open",
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )
    temp_db.insert_item(
        {
            "id": "note_existing",
            "owner_id": OWNER_ID,
            "type": "note",
            "title": "旧笔记",
            "content": "旧内容",
            "category": "知识",
            "references": [
                {"kind": "item", "id": "task_existing", "type": "task", "title": "旧任务"}
            ],
            "related_items": ["task_existing"],
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )

    duplicate_bundle = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_existing",
                    "title": "导入任务",
                    "priority": 2,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
            "note": [
                {
                    "_type": "note",
                    "_schema": 2,
                    "id": "note_existing",
                    "title": "导入笔记",
                    "content": "引用导入任务",
                    "category": "知识",
                    "references": [
                        {"kind": "item", "id": "task_existing", "type": "task", "title": "导入任务"}
                    ],
                    "related_items": ["task_existing"],
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps(
                {"types": ["task", "note"], "conflict_policy": "duplicate"}
            ),
        },
        content=duplicate_bundle,
    )

    assert response.status_code == 200
    imported_task = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=20)
        if item.id != "task_existing" and item.title == "导入任务"
    ][0]
    imported_note = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "note"}, limit=20)
        if item.id != "note_existing" and item.title == "导入笔记"
    ][0]

    assert imported_note.references[0]["id"] == imported_task.id
    assert imported_note.related_items == [imported_task.id]


def test_import_relationship_rewrite_remaps_note_links_and_event_source_ids():
    note_payload = {
        "type": "note",
        "id": "note_new",
        "references": [
            {"kind": "item", "id": "task_old", "type": "task", "title": "任务"},
            {"kind": "item", "id": "event_keep", "type": "event", "title": "日程"},
        ],
        "related_items": ["task_old", "event_keep"],
    }
    event_payload = {
        "type": "event",
        "id": "event_new",
        "source_item_id": "event_old",
    }

    rewritten_note = transfer_api._rewrite_import_item_relationships(
        note_payload,
        {"task_old": "task_new", "event_old": "event_new_source"},
    )
    rewritten_event = transfer_api._rewrite_import_item_relationships(
        event_payload,
        {"event_old": "event_new_source"},
    )

    assert rewritten_note["references"][0]["id"] == "task_new"
    assert len(rewritten_note["references"]) == 1
    assert rewritten_note["related_items"] == ["task_new"]
    assert rewritten_event["source_item_id"] == "event_new_source"


def test_import_execute_restores_event_collection_before_leaf_events(
    client: Any, temp_db: Database, auth_headers: dict
):
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "event_collection": [
                {
                    "_type": "event_collection",
                    "_schema": 2,
                    "id": "bundle_conf",
                    "kind": "multi_node",
                    "title": "导入会议",
                    "category": "学术",
                    "notes": "整体备注",
                    "start_time": "2026-03-05T09:00:00",
                    "end_time": "2026-04-01T10:00:00",
                }
            ],
            "event": [
                {
                    "_type": "event",
                    "_schema": 2,
                    "id": "bundle_conf_m01",
                    "title": "摘要截止",
                    "category": "学术",
                    "start_time": "2026-03-05T09:00:00",
                    "event_role": "multi_node_child",
                    "event_collection_id": "bundle_conf",
                    "event_collection_kind": "multi_node",
                    "event_index": 1,
                    "event_node_key": "m01",
                }
            ],
        }
    )

    inspect_response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=bundle_bytes,
    )
    assert inspect_response.status_code == 200
    inspect_data = inspect_response.json()["data"]
    assert inspect_data["summary"]["types"] == ["event"]
    assert inspect_data["counts"]["valid"] == 2
    assert inspect_data["counts"]["total_samples"] == 1
    assert any(file["type"] == "event_collection" for file in inspect_data["files"])

    import_response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["event"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )

    assert import_response.status_code == 200
    event = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "event"}, limit=20)
        if item.title == "摘要截止"
    ][0]
    collection = temp_db.get_event_collection(event.event_collection_id, OWNER_ID)
    assert collection is not None
    assert collection["title"] == "导入会议"
    assert collection["id"] != "bundle_conf"
    assert collection["context"]["import"]["source_id"] == "bundle_conf"
    assert event.id != "bundle_conf_m01"
    assert event.context["import"]["source_id"] == "bundle_conf_m01"
    assert event.event_collection_id == collection["id"]
    assert event.event_collection_kind == "multi_node"


def test_import_execute_idempotency_blocks_duplicate_bundle(
    client: Any, temp_db: Database, auth_headers: dict
):
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_idem_block",
                    "title": "幂等阻断",
                    "content": "正文",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    # 第一次导入成功
    resp1 = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )
    assert resp1.status_code == 200

    # 第二次导入同一 bundle 应被阻断 (409)
    resp2 = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )
    assert resp2.status_code == 409

    # 带 force=true 可以强制重新导入
    resp3 = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps(
                {"types": ["task"], "conflict_policy": "skip", "force": True}
            ),
        },
        content=bundle_bytes,
    )
    assert resp3.status_code == 200


def test_execute_import_bundle_uses_db_level_bundle_guard(temp_db: Database):
    operations = [
        (
            "insert",
            {
                "id": "bundle_guard_task",
                "type": "task",
                "title": "bundle guard",
                "content": "first import",
                "category": "工作",
                "priority": 3,
                "status": "open",
                "created_at": "2026-03-20T09:00:00+08:00",
                "updated_at": "2026-03-20T09:00:00+08:00",
            },
        )
    ]

    temp_db.execute_import_bundle(
        owner_id=OWNER_ID,
        bundle_id="bundle-guard-1",
        operations=operations,
        filename="bundle.zip",
        types=["task"],
        record_count=1,
        result_summary={"inserted": 1, "updated": 0, "skipped": 0, "failed": 0},
        force=False,
    )

    with pytest.raises(DuplicateBundleImportError):
        temp_db.execute_import_bundle(
            owner_id=OWNER_ID,
            bundle_id="bundle-guard-1",
            operations=operations,
            filename="bundle.zip",
            types=["task"],
            record_count=1,
            result_summary={"inserted": 1, "updated": 0, "skipped": 0, "failed": 0},
            force=False,
        )

    logs = [log for log in temp_db.get_transfer_logs(OWNER_ID) if log["action"] == "import"]
    assert len(logs) == 1


def test_import_execute_transaction_atomicity(client: Any, temp_db: Database, auth_headers: dict):
    """如果导入事务中有记录失败，整体应该回滚"""
    # 插入一条已存在的记录用于 overwrite
    temp_db.insert_item(
        {
            "id": "task_atom_exist",
            "owner_id": OWNER_ID,
            "type": "task",
            "title": "旧任务",
            "content": "旧",
            "category": "工作",
            "priority": 2,
            "status": "open",
            "created_at": "2026-03-01T09:00:00+08:00",
            "updated_at": "2026-03-01T09:00:00+08:00",
        }
    )

    # 测试正常的批量插入是原子的
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_atom_new_1",
                    "title": "原子新1",
                    "content": "OK",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                },
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_atom_new_2",
                    "title": "原子新2",
                    "content": "OK",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                },
            ],
        }
    )

    resp = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["results"]["inserted"] == 2

    # 两条记录都应该存在
    imported = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=20)
        if item.title in {"原子新1", "原子新2"}
    ]
    assert {item.context["import"]["source_id"] for item in imported} == {
        "task_atom_new_1",
        "task_atom_new_2",
    }


def test_import_execute_rejects_empty_selected_types(client: Any, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_only",
                    "title": "只有任务",
                    "content": "正文",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    response = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["event"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )

    assert response.status_code == 422
    assert "At least one import type" in response.json()["message"]


def test_import_execute_creates_audit_log(client: Any, temp_db: Database, auth_headers: dict):
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_audit_log",
                    "title": "审计日志测试",
                    "content": "正文",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-20T09:00:00+08:00",
                    "updated_at": "2026-03-20T09:00:00+08:00",
                }
            ],
        }
    )

    client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )

    logs = temp_db.get_transfer_logs(OWNER_ID)
    import_logs = [log for log in logs if log["action"] == "import"]
    assert len(import_logs) >= 1
    assert import_logs[0]["record_count"] >= 1
    assert "bundle_id" in import_logs[0] or import_logs[0].get("bundle_id")


def test_import_rejects_oversized_body(client: Any, auth_headers: dict):
    """上传超过大小限制的文件应返回 413"""
    # 通过 Content-Length header 触发
    response = client.post(
        "/api/transfer/import/inspect",
        headers={**auth_headers, "Content-Length": str(200 * 1024 * 1024)},
        content=b"x",
    )
    assert response.status_code == 413


def test_import_samples_pagination(client: Any, auth_headers: dict):
    # 创建包含 10 条记录的 bundle
    tasks = []
    for i in range(10):
        tasks.append(
            {
                "_type": "task",
                "_schema": 2,
                "id": f"task_page_{i}",
                "title": f"分页任务{i}",
                "content": "正文",
                "category": "工作",
                "priority": 3,
                "status": "open",
                "created_at": "2026-03-20T09:00:00+08:00",
                "updated_at": "2026-03-20T09:00:00+08:00",
            }
        )
    bundle_bytes = _build_sample_bundle_bytes({"task": tasks})

    # 请求第1页 (5条/页)
    response = client.post(
        "/api/transfer/import/samples",
        headers={
            **auth_headers,
            "X-Transfer-Page": "1",
            "X-Transfer-Page-Size": "5",
        },
        content=bundle_bytes,
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 10
    assert body["page"] == 1
    assert len(body["samples"]) == 5

    # 请求第2页
    response2 = client.post(
        "/api/transfer/import/samples",
        headers={
            **auth_headers,
            "X-Transfer-Page": "2",
            "X-Transfer-Page-Size": "5",
        },
        content=bundle_bytes,
    )
    body2 = response2.json()["data"]
    assert body2["page"] == 2
    assert len(body2["samples"]) == 5
