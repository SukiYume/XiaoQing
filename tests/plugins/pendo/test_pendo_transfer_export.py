"""导出范围、预览和下载。"""

from __future__ import annotations

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from tests.helpers.pendo_web_transfer_test_support import (
    OWNER_ID,
    Any,
    Database,
    ZoneInfo,
    _build_sample_bundle_bytes,
    _ImportRequest,
    _seed_items,
    datetime,
    io,
    json,
    pytest,
    timezone,
    transfer_api,
    zipfile,
)

auth_headers = _fixture_support.auth_headers


def test_export_preview_rejects_reversed_custom_range(client: Any, auth_headers: dict):
    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["task"],
                "preset": "custom",
                "start": "2026-03-31",
                "end": "2026-03-01",
            }
        },
    )

    assert response.status_code == 422
    assert "before end" in response.json()["message"]


def test_resolve_range_supports_quarter_to_date():
    start, end = transfer_api.resolve_range(
        transfer_api.ExportSelection(types=["task"], preset="quarter"),
        now=datetime(2026, 3, 30, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert start.isoformat() == "2026-01-01"
    assert end.isoformat() == "2026-03-30"


def test_resolve_range_rejects_invalid_timezone():
    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api.resolve_range(
            transfer_api.ExportSelection(types=["task"], preset="month", timezone="Mars/Olympus"),
            now=datetime(2026, 3, 30, 9, 0, 0),
        )

    assert exc_info.value.status_code == 422
    assert "Invalid timezone" in exc_info.value.detail


def test_resolve_range_rejects_naive_clock_instead_of_using_host_timezone():
    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api.resolve_range(
            transfer_api.ExportSelection(types=["task"], preset="month", timezone="Asia/Shanghai"),
            now=datetime(2026, 3, 30, 9, 0, 0),
        )

    assert exc_info.value.status_code == 422
    assert "timezone-aware" in exc_info.value.detail


@pytest.mark.parametrize(
    "selection,expected_detail",
    [
        ({"preset": "unknown"}, "Unsupported export preset"),
        ({"preset": "custom", "start": "2026-03-01"}, "requires start and end"),
        (
            {"preset": "custom", "start": "2026/03/01", "end": "2026-03-31"},
            "Invalid date",
        ),
        (
            {"preset": "custom", "start": "2026-02-30", "end": "2026-03-31"},
            "Invalid date",
        ),
    ],
)
def test_resolve_range_rejects_unsupported_or_malformed_ranges(
    selection: dict[str, Any], expected_detail: str
) -> None:
    """自定义范围必须完整，并使用真实存在的 ISO 日期。"""

    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api.resolve_range(
            transfer_api.ExportSelection(types=["task"], **selection),
            now=datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc),
        )

    assert exc_info.value.status_code == 422
    assert expected_detail in str(exc_info.value.detail)


def test_timezone_and_date_coercion_cover_blank_offset_and_invalid_values() -> None:
    """空白时区采用默认值，偏移时间先换算时区，坏值不参与范围过滤。"""

    assert transfer_api._resolve_timezone("   ").key == transfer_api.DEFAULT_TIMEZONE
    assert (
        transfer_api._coerce_date(
            "2026-03-01T01:00:00Z", ZoneInfo("America/Los_Angeles")
        ).isoformat()
        == "2026-02-28"
    )
    assert transfer_api._coerce_date("   ") is None
    assert transfer_api._coerce_date("not-a-date") is None

    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api._resolve_timezone("x" * 129)
    assert exc_info.value.status_code == 422


def test_items_without_effective_dates_do_not_match_bounded_exports() -> None:
    """缺少有效日期的日程和普通条目不能误入有限日期导出。"""

    start = datetime(2026, 3, 1).date()
    end = datetime(2026, 3, 31).date()

    assert transfer_api.item_matches_range(object(), "event", start, end) is False
    assert transfer_api.item_matches_range(object(), "task", start, end) is False


def test_export_type_selection_is_ordered_deduplicated_and_nonempty() -> None:
    """类型选择保留首现顺序，并在查询前拒绝空集和未知类型。"""

    selection = transfer_api.ExportSelection(types=["task", "task", "note"])
    assert transfer_api._normalize_selection(selection) == ["task", "note"]

    for invalid_types in ([], ["unknown"]):
        with pytest.raises(transfer_api.HTTPException) as exc_info:
            transfer_api._normalize_selection(transfer_api.ExportSelection(types=invalid_types))
        assert exc_info.value.status_code == 422

    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api.query_items_for_types(object(), OWNER_ID, ["unknown"])
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"selection": {"types": ["task"], "preset": "all", "extra": True}},
        {"selection": {"types": ["task"], "preset": "all"}, "extra": True},
    ],
)
def test_export_requests_reject_unknown_fields(
    client: Any, auth_headers: dict[str, str], payload: dict[str, Any]
) -> None:
    """导出请求的外层和选择层都不静默吞掉拼错的字段。"""

    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json=payload,
    )

    assert response.status_code == 422


def test_export_preview_returns_counts_by_type_and_filters_by_time_field(
    client: Any, temp_db: Database, auth_headers: dict
):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["event", "task", "ledger", "note", "diary"],
                "preset": "custom",
                "start": "2026-03-01",
                "end": "2026-03-31",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["counts"] == {"event": 1, "task": 2, "ledger": 1, "note": 1, "diary": 1}
    assert body["total"] == 6


def test_export_preview_returns_warnings_field(client: Any, temp_db: Database, auth_headers: dict):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/preview",
        headers=auth_headers,
        json={"selection": {"types": ["task"], "preset": "all"}},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert "warnings" in body
    assert isinstance(body["warnings"], list)


def test_export_download_returns_bundle_with_manifest(
    client: Any, temp_db: Database, auth_headers: dict
):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/download",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["task", "note"],
                "preset": "custom",
                "start": "2026-03-01",
                "end": "2026-03-31",
            }
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert ".pendo.zip" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content), "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "data/tasks.ndjson" in names
        assert "data/notes.ndjson" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["selection"]["types"] == ["task", "note"]
        assert "bundle_id" in manifest


def test_export_download_includes_event_collections_for_event_graph(
    client: Any, temp_db: Database, auth_headers: dict
):
    temp_db.create_event_collection(
        {
            "id": "conf_2026",
            "owner_id": OWNER_ID,
            "kind": "multi_node",
            "title": "FRB2026会议",
            "category": "学术",
            "notes": "整体会议",
            "start_time": "2026-03-05T09:00:00",
            "end_time": "2026-04-01T10:00:00",
        }
    )
    temp_db.insert_item(
        {
            "id": "conf_2026_m01",
            "owner_id": OWNER_ID,
            "type": "event",
            "title": "摘要截止",
            "category": "学术",
            "start_time": "2026-03-05T09:00:00",
            "event_role": "multi_node_child",
            "event_collection_id": "conf_2026",
            "event_collection_kind": "multi_node",
            "event_index": 1,
            "event_node_key": "m01",
        }
    )

    response = client.post(
        "/api/transfer/export/download",
        headers=auth_headers,
        json={
            "selection": {
                "types": ["event"],
                "preset": "custom",
                "start": "2026-03-01",
                "end": "2026-03-31",
            }
        },
    )

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content), "r") as zf:
        names = set(zf.namelist())
        assert "data/events.ndjson" in names
        assert "data/event_collections.ndjson" in names
        event_rows = [
            json.loads(line)
            for line in zf.read("data/events.ndjson").decode("utf-8").splitlines()
            if line
        ]
        collection_rows = [
            json.loads(line)
            for line in zf.read("data/event_collections.ndjson").decode("utf-8").splitlines()
            if line
        ]

    assert event_rows[0]["event_collection_id"] == "conf_2026"
    assert event_rows[0]["event_collection_kind"] == "multi_node"
    assert collection_rows[0]["_type"] == "event_collection"
    assert collection_rows[0]["title"] == "FRB2026会议"


def test_export_record_validation_distinguishes_unknown_and_invalid_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无规范的扩展类型不告警，已知类型的历史坏字段会产生可定位告警。"""

    monkeypatch.setattr(transfer_api, "get_item_normalizer", lambda _item_type: None)
    assert transfer_api._export_record_warning({"_type": "extension"}) is None

    def reject_record(_record: dict[str, Any], *, partial: bool) -> None:
        assert partial is True
        raise ValueError("invalid historical field")

    monkeypatch.setattr(
        transfer_api,
        "get_item_normalizer",
        lambda _item_type: reject_record,
    )
    warning = transfer_api._export_record_warning({"_type": "task", "id": "legacy"})
    assert warning == "task/legacy: 记录字段校验失败"


def test_export_dataset_keeps_serialized_records_when_compatibility_warns(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容性告警只提示历史字段，不得丢弃用户导出记录。"""

    _seed_items(temp_db)
    monkeypatch.setattr(
        transfer_api,
        "_export_record_warning",
        lambda record: f"{record['id']}: warning",
    )

    records, counts, _bounds, warnings = transfer_api._build_export_dataset(
        temp_db,
        OWNER_ID,
        transfer_api.ExportSelection(types=["task"], preset="all"),
    )

    assert counts["task"] == 3
    assert len(records["task"]) == 3
    assert len(warnings) == 3


def test_event_collection_export_uses_one_batch_lookup_and_warns_for_missing_headers(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个日程叶子只批量读取一次集合头，缺失集合则保留来源告警。"""

    temp_db.create_event_collection(
        {
            "id": "collection-present",
            "owner_id": OWNER_ID,
            "kind": "multi_node",
            "title": "存在的集合",
        }
    )
    real_lookup = temp_db.get_event_collections_by_ids
    calls: list[tuple[str, list[str]]] = []

    def track_lookup(owner_id: str, collection_ids: list[str]) -> dict[str, dict[str, Any]]:
        calls.append((owner_id, collection_ids))
        return real_lookup(owner_id, collection_ids)

    monkeypatch.setattr(temp_db, "get_event_collections_by_ids", track_lookup)
    warnings: list[str] = []
    records = transfer_api._collect_event_collection_records(
        temp_db,
        OWNER_ID,
        [
            {"id": "event-1", "event_collection_id": "collection-present"},
            {"id": "event-2", "event_collection_id": "collection-present"},
            {"id": "event-3", "event_collection_id": "collection-missing"},
            {"id": "event-4"},
        ],
        warnings,
    )

    assert calls == [
        (OWNER_ID, ["collection-present", "collection-missing"]),
    ]
    assert [record["id"] for record in records] == ["collection-present"]
    assert warnings == ["event/event-3: missing event collection collection-missing"]


def test_bundle_manifest_count_is_derived_from_written_records() -> None:
    """传输包清单计数只由实际写入记录决定，避免双份计数状态漂移。"""

    records = {
        "task": [
            {"_type": "task", "_schema": 2, "id": "task-1", "title": "一"},
            {"_type": "task", "_schema": 2, "id": "task-2", "title": "二"},
        ]
    }
    bundle = transfer_api._build_bundle_bytes(
        records,
        transfer_api.ExportSelection(types=["task"], preset="all"),
        None,
        None,
    )

    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["files"][0]["count"] == len(records["task"])


def test_export_download_creates_audit_log(client: Any, temp_db: Database, auth_headers: dict):
    _seed_items(temp_db)

    client.post(
        "/api/transfer/export/download",
        headers=auth_headers,
        json={"selection": {"types": ["task"], "preset": "all"}},
    )

    logs = temp_db.get_transfer_logs(OWNER_ID)
    assert len(logs) >= 1
    assert logs[0]["action"] == "export"
    assert "task" in logs[0]["types"]


@pytest.mark.parametrize(
    "raw_options",
    [
        "{",
        "[]",
        json.dumps({"force": "false"}),
        json.dumps({"unknown": True}),
        json.dumps({"types": "task"}),
        json.dumps({"types": [1]}),
        json.dumps({"types": None}),
        json.dumps({"conflict_policy": "replace"}),
        json.dumps({"invalid_policy": "continue"}),
    ],
)
def test_import_options_reject_malformed_or_implicitly_coerced_values(
    raw_options: str,
) -> None:
    """导入控制头必须是严格对象，尤其不能把字符串 ``false`` 当成真值。"""

    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api._parse_import_options(raw_options)

    assert exc_info.value.status_code == 422


def test_import_options_normalize_duplicate_types_without_inventing_defaults() -> None:
    """未提供头时保留空选项，显式类型则稳定去重并保留严格布尔值。"""

    assert transfer_api._parse_import_options(None) == {}
    assert transfer_api._parse_import_options(
        json.dumps({"types": ["task", "task", "note"], "force": False})
    ) == {"types": ["task", "note"], "force": False}


def test_selected_import_types_distinguish_omitted_duplicate_and_empty_choices() -> None:
    """省略类型表示全选，重复选择只导入一次，显式空集必须报错。"""

    parsed, _records, _errors = transfer_api._inspect_bundle_data(
        _build_sample_bundle_bytes(
            {
                "task": [{"_type": "task", "_schema": 2, "title": "任务"}],
                "note": [{"_type": "note", "_schema": 2, "title": "笔记"}],
            }
        )
    )

    assert transfer_api._selected_import_types({}, parsed) == ["task", "note"]
    assert transfer_api._selected_import_types({"types": ["task", "task", "note"]}, parsed) == [
        "task",
        "note",
    ]
    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api._selected_import_types({"types": []}, parsed)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_upload_body_rejects_empty_and_actual_oversized_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无效长度头不绕过空包检查，实际字节数也必须独立受限。"""

    with pytest.raises(transfer_api.HTTPException) as empty_error:
        await transfer_api._read_upload_body(
            _ImportRequest(b"", {"content-length": "not-an-integer"})
        )
    assert empty_error.value.status_code == 422

    monkeypatch.setattr(transfer_api, "MAX_UPLOAD_SIZE", 3)
    with pytest.raises(transfer_api.HTTPException) as size_error:
        await transfer_api._read_upload_body(_ImportRequest(b"four"))
    assert size_error.value.status_code == 413
