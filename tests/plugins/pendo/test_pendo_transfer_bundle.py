"""传输包格式、清单和读取边界。"""

from __future__ import annotations

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from tests.helpers.pendo_web_transfer_test_support import (
    OWNER_ID,
    Any,
    BundleValidationError,
    Database,
    DiaryItem,
    EventItem,
    LedgerItem,
    NoteItem,
    TaskItem,
    _build_raw_bundle,
    _build_sample_bundle_bytes,
    _build_single_member_manifest,
    _ImportRequest,
    _seed_items,
    _simple_task_bundle,
    asyncio,
    build_manifest,
    hashlib,
    io,
    json,
    pytest,
    read_bundle,
    serialize_event_collection,
    serialize_item,
    threading,
    time,
    transfer_api,
    transfer_bundle_module,
    write_bundle,
    zipfile,
)

auth_headers = _fixture_support.auth_headers


def test_web_validation_errors_use_safe_response_shape(client: Any, auth_headers: dict):
    response = client.post(
        "/api/items",
        headers = auth_headers,
        json    = {"type": "ledger", "title": "坏账", "amount": "not-number"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "请求参数校验失败"
    assert body["error_code"] == "validation_error"
    assert "not-number" not in str(body)
    assert "input" not in str(body)


@pytest.mark.asyncio
async def test_import_inspect_parsing_does_not_block_event_loop(
    temp_db: Database,
    monkeypatch,
):
    bundle       = _simple_task_bundle("inspect-offloop")
    real_inspect = transfer_api._inspect_bundle_data
    release      = threading.Event()
    heartbeat    = asyncio.Event()
    loop         = asyncio.get_running_loop()
    entered_at   = 0.0

    def slow_inspect(payload: bytes):
        nonlocal entered_at
        entered_at = time.monotonic()
        loop.call_soon_threadsafe(heartbeat.set)
        release.wait(timeout=1)
        return real_inspect(payload)

    monkeypatch.setattr(transfer_api, "_inspect_bundle_data", slow_inspect)
    fallback_release = threading.Timer(0.5, release.set)
    fallback_release.start()
    try:
        task = asyncio.create_task(
            transfer_api.inspect_import(_ImportRequest(bundle), owner_id=OWNER_ID, db=temp_db)
        )
        await asyncio.wait_for(heartbeat.wait(), timeout=0.75)
        heartbeat_delay = time.monotonic() - entered_at
        release.set()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
        fallback_release.cancel()

    assert heartbeat_delay < 0.2
    assert result["data"]["counts"]["valid"] == 1


@pytest.mark.asyncio
async def test_import_planning_and_transaction_do_not_block_event_loop(
    temp_db: Database,
    monkeypatch,
):
    bundle       = _simple_task_bundle("execute-offloop")
    real_execute = temp_db.execute_import_bundle
    release      = threading.Event()
    heartbeat    = asyncio.Event()
    loop         = asyncio.get_running_loop()
    entered_at   = 0.0

    def slow_execute(**kwargs):
        nonlocal entered_at
        entered_at = time.monotonic()
        loop.call_soon_threadsafe(heartbeat.set)
        release.wait(timeout=1)
        return real_execute(**kwargs)

    monkeypatch.setattr(temp_db, "execute_import_bundle", slow_execute)
    fallback_release = threading.Timer(0.5, release.set)
    fallback_release.start()
    try:
        task = asyncio.create_task(
            transfer_api.execute_import(
                _ImportRequest(bundle),
                x_transfer_options = json.dumps({"types": ["task"]}),
                owner_id           = OWNER_ID,
                db                 = temp_db,
            )
        )
        await asyncio.wait_for(heartbeat.wait(), timeout=0.75)
        heartbeat_delay = time.monotonic() - entered_at
        release.set()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
        fallback_release.cancel()

    assert heartbeat_delay < 0.2
    assert result["data"]["results"]["inserted"] == 1


def test_build_manifest_includes_bundle_id():
    manifest = build_manifest(
        {"types": ["event", "task"], "preset": "month", "start": "2026-03-01", "end": "2026-03-31"},
        [{"path": "data/tasks.ndjson", "type": "task", "count": 1, "sha256": "a" * 64}],
        "Asia/Shanghai",
    )
    assert manifest["format"] == "pendo-bundle"
    assert manifest["version"] == 2
    assert manifest["selection"]["preset"] == "month"
    assert "bundle_id" in manifest
    assert len(manifest["bundle_id"]) == 32


def test_build_manifest_rejects_non_iana_source_timezone():
    with pytest.raises(BundleValidationError, match="Invalid source timezone"):
        build_manifest(
            {"types": ["task"], "preset": "all", "start": None, "end": None},
            [],
            "Mars/Olympus",
        )


def test_build_manifest_and_serialize_item_preserve_type_fields():
    manifest = build_manifest(
        {"types": ["event", "task"], "preset": "month", "start": "2026-03-01", "end": "2026-03-31"},
        [{"path": "data/tasks.ndjson", "type": "task", "count": 1, "sha256": "a" * 64}],
        "Asia/Shanghai",
    )

    assert manifest["format"] == "pendo-bundle"
    assert manifest["version"] == 2
    assert manifest["selection"]["preset"] == "month"

    event_record = serialize_item(
        EventItem(
            id                    = "event_1",
            owner_id              = OWNER_ID,
            title                 = "发布会",
            category              = "工作",
            start_time            = "2026-03-20T09:00:00+08:00",
            end_time              = "2026-03-20T10:00:00+08:00",
            timezone              = "Asia/Shanghai",
            participants          = ["A"],
            remind_times          = ["2026-03-20T08:30:00+08:00"],
            notes                 = "带录音",
            event_role            = "multi_node_child",
            event_collection_id   = "col_1",
            event_collection_kind = "multi_node",
            event_index           = 1,
            event_node_key        = "m01",
        )
    )
    collection_record = serialize_event_collection(
        {
            "id": "col_1",
            "owner_id": OWNER_ID,
            "kind": "multi_node",
            "title": "发布会整体",
            "category": "工作",
            "notes": "整体备注",
            "start_time": "2026-03-20T09:00:00+08:00",
            "end_time": "2026-03-20T10:00:00+08:00",
        }
    )
    task_record = serialize_item(
        TaskItem(
            id           = "task_1",
            owner_id     = OWNER_ID,
            title        = "补图表",
            content      = "导出实现",
            category     = "工作",
            plan_date    = "2026-03-21",
            deadline_at  = "2026-03-21T18:00:00+08:00",
            priority     = 2,
            status       = "open",
            remind_times = ["2026-03-21T09:00:00+08:00"],
        )
    )
    note_record = serialize_item(
        NoteItem(
            id            = "note_1",
            owner_id      = OWNER_ID,
            title         = "格式说明",
            content       = "记录规范",
            category      = "知识",
            tags          = ["格式"],
            references    = [{"kind": "item", "id": "task_1"}],
            related_items = ["event_1"],
        )
    )
    diary_record = serialize_item(
        DiaryItem(
            id               = "diary_1",
            owner_id         = OWNER_ID,
            title            = "今天",
            content          = "正文",
            diary_date       = "2026-03-21",
            mood             = "happy",
            mood_score       = 9,
            weather          = "sunny",
            location         = "家",
            template_id      = "tpl-1",
            entry_time       = "2026-03-21T22:15:00",
            template_answers = [{"prompt": "今天做了什么", "answer": "写日记"}],
            is_favorite      = True,
        )
    )
    ledger_record = serialize_item(
        LedgerItem(
            id               = "ledger_1",
            owner_id         = OWNER_ID,
            title            = "咖啡",
            amount           = 18,
            amount_cents     = 1800,
            transaction_type = "expense",
            currency         = "CNY",
            ledger_category  = "餐饮",
            ledger_date      = "2026-03-21",
            account_name     = "微信",
            merchant         = "咖啡店",
            remark           = "拿铁",
        )
    )

    assert event_record["participants"] == ["A"]
    assert "milestones" not in event_record
    assert "rrule" not in event_record
    assert "parent_id" not in event_record
    assert event_record["event_collection_id"] == "col_1"
    assert event_record["event_collection_kind"] == "multi_node"
    assert event_record["event_index"] == 1
    assert collection_record["_type"] == "event_collection"
    assert collection_record["title"] == "发布会整体"
    assert "owner_id" not in collection_record
    assert task_record["plan_date"] == "2026-03-21"
    assert task_record["deadline_at"] == "2026-03-21T18:00:00+08:00"
    assert "subtasks" not in task_record
    assert "dependencies" not in task_record
    assert note_record["references"][0]["id"] == "task_1"
    assert diary_record["mood_score"] == 9
    assert diary_record["entry_time"] == "2026-03-21T22:15:00"
    assert diary_record["template_answers"][0]["answer"] == "写日记"
    assert diary_record["is_favorite"] is True
    assert ledger_record["ledger_category"] == "餐饮"
    assert ledger_record["amount_cents"] == 1800
    assert ledger_record["transaction_type"] == "expense"
    assert ledger_record["account_name"] == "微信"
    assert "direction" not in ledger_record


def test_transfer_serializers_reject_unsupported_types_and_fields():
    with pytest.raises(BundleValidationError, match="Unsupported item type"):
        serialize_item({"type": "unknown", "title": "bad"})
    with pytest.raises(BundleValidationError, match="Unsupported record type"):
        transfer_bundle_module.deserialize_record({"_type": "unknown"})
    with pytest.raises(BundleValidationError, match="Unsupported field for event_collection"):
        transfer_bundle_module.deserialize_event_collection_record(
            {"_type": "event_collection", "unsupported": True}
        )


def test_read_bundle_rejects_missing_manifest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "data/tasks.ndjson", '{"_type":"task","_schema":2,"id":"task_1","title":"测试"}\n'
        )
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="manifest.json"):
        read_bundle(buf)


def test_read_bundle_rejects_invalid_zip_as_validation_error():
    with pytest.raises(BundleValidationError, match="Invalid bundle zip"):
        read_bundle(io.BytesIO(b"not-a-zip"))


def test_read_bundle_rejects_invalid_manifest_json_as_validation_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", b"{not-json")
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="manifest"):
        read_bundle(buf)


@pytest.mark.parametrize("manifest", [[], "not-an-object", 2, None])
def test_read_bundle_rejects_non_object_manifest(manifest: object):
    buf = _build_raw_bundle(manifest, [])

    with pytest.raises(BundleValidationError, match="JSON object"):
        read_bundle(buf)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format", "other", "Unsupported bundle format"),
        ("version", 2.0, "Unsupported bundle version"),
        ("bundle_id", "", "Bundle id"),
        ("exported_at", [], "exported_at"),
        ("attachments_mode", "embedded", "attachments mode"),
        ("selection", None, "selection must be an object"),
        ("selection", {}, "selection types must be a list"),
        ("source", None, "source must be an object"),
        ("source", {}, "source timezone is required"),
        ("files", {}, "files must be a list"),
    ],
)
def test_read_bundle_rejects_invalid_manifest_field(
    field: str,
    value: object,
    message: str,
):
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    manifest[field] = value

    with pytest.raises(BundleValidationError, match=message):
        read_bundle(_build_raw_bundle(manifest, []))


def test_read_bundle_rejects_non_object_file_entry():
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    manifest["files"] = [1]

    with pytest.raises(BundleValidationError, match="files must contain objects"):
        read_bundle(_build_raw_bundle(manifest, []))


@pytest.mark.parametrize("raw_record", [[], "not-an-object", 2, None])
def test_read_bundle_reports_non_object_ndjson_record(raw_record: object):
    content = (json.dumps(raw_record, ensure_ascii=False) + "\n").encode("utf-8")
    manifest = _build_single_member_manifest("task", content)

    parsed = read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))

    assert parsed.records_by_type == {}
    assert parsed.file_summaries[0]["count"] == 1
    assert parsed.file_summaries[0]["valid"] == 0
    assert parsed.errors[0]["message"].endswith("must be a JSON object")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"_type": "note", "_schema": 2}, "Record type mismatch"),
        ({"_type": "task", "_schema": 2.0}, "Unsupported schema"),
    ],
)
def test_read_bundle_reports_invalid_record_metadata(
    record: dict[str, Any],
    message: str,
):
    content = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    manifest = _build_single_member_manifest("task", content)

    parsed = read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))

    assert parsed.records_by_type == {}
    assert message in parsed.errors[0]["message"]


def test_read_bundle_accepts_missing_optional_summaries_and_record_metadata():
    content  = b'{"id":"task_external","title":"External task"}\n'
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [{"path": "data/tasks.ndjson", "type": "task"}],
        "Asia/Shanghai",
    )

    parsed = read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))

    assert parsed.records_by_type["task"][0]["id"] == "task_external"
    assert parsed.errors == []
    assert parsed.warnings == [
        "data/tasks.ndjson: 缺少 sha256 校验和，跳过完整性检查",
        "data/tasks.ndjson: 缺少 count 字段，跳过行数校验",
    ]


def test_read_bundle_infers_file_type_when_manifest_entry_omits_it():
    content  = b'{"id":"task_inferred","title":"Inferred"}\n'
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/tasks.ndjson",
                "count": 1,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )

    parsed = read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))

    assert parsed.records_by_type["task"][0]["id"] == "task_inferred"


def test_read_bundle_enforces_runtime_record_limit_without_declared_count(monkeypatch):
    monkeypatch.setattr(transfer_bundle_module, "MAX_IMPORT_RECORDS", 0)
    content  = b'\n{"_type":"task","_schema":2}\n'
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/tasks.ndjson",
                "type": "task",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )

    with pytest.raises(transfer_bundle_module.BundleRecordLimitError):
        read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))


def test_read_bundle_rejects_missing_declared_member():
    content  = b'{"_type":"task","_schema":2}\n'
    manifest = _build_single_member_manifest("task", content)

    with pytest.raises(BundleValidationError, match="Bundle is missing data/tasks.ndjson"):
        read_bundle(_build_raw_bundle(manifest, []))


def test_read_bundle_rejects_checksum_mismatch():
    expected = b'{"_type":"task","_schema":2,"id":"expected"}\n'
    actual   = b'{"_type":"task","_schema":2,"id":"changed"}\n'
    manifest = _build_single_member_manifest("task", expected)

    with pytest.raises(BundleValidationError, match="Checksum mismatch"):
        read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", actual)]))


def test_read_bundle_rejects_invalid_member_utf8():
    content  = b"\xff\n"
    manifest = _build_single_member_manifest("task", content)

    with pytest.raises(BundleValidationError, match="not valid UTF-8"):
        read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))


def test_read_bundle_rejects_actual_count_mismatch():
    content = b'{"_type":"task","_schema":2}\n'
    manifest = _build_single_member_manifest("task", content, count=2)

    with pytest.raises(BundleValidationError, match="Count mismatch"):
        read_bundle(_build_raw_bundle(manifest, [("data/tasks.ndjson", content)]))


def test_read_bundle_rejects_duplicate_archive_member():
    content  = b'{"_type":"task","_schema":2,"id":"task_1"}\n'
    manifest = _build_single_member_manifest("task", content)
    with pytest.warns(UserWarning, match="Duplicate name"):
        buf = _build_raw_bundle(
            manifest,
            [("data/tasks.ndjson", content), ("data/tasks.ndjson", content)],
        )

    with pytest.raises(BundleValidationError, match="Duplicate archive member"):
        read_bundle(buf)


def test_read_bundle_rejects_encrypted_archive_member_flag():
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    archive        = bytearray(_build_raw_bundle(manifest, []).getvalue())
    central_header = archive.find(b"PK\x01\x02")
    assert central_header >= 0
    flag_offset = central_header + 8
    flags = int.from_bytes(archive[flag_offset : flag_offset + 2], "little") | 0x1
    archive[flag_offset : flag_offset + 2] = flags.to_bytes(2, "little")

    with pytest.raises(BundleValidationError, match="Encrypted archive member"):
        read_bundle(io.BytesIO(archive))


def test_read_bundle_maps_corrupt_member_crc_to_validation_error():
    content  = b'{"_type":"task","_schema":2,"id":"crc-corruption"}\n'
    manifest = _build_single_member_manifest("task", content)
    buf      = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/tasks.ndjson", content)
    archive        = bytearray(buf.getvalue())
    content_offset = archive.find(content)
    assert content_offset >= 0
    archive[content_offset] ^= 0x1

    with pytest.raises(BundleValidationError, match="Cannot read archive member"):
        read_bundle(io.BytesIO(archive))


def test_read_bundle_rejects_unmanifested_archive_member():
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    buf = _build_raw_bundle(manifest, [("data/unlisted.ndjson", b"{}\n")])

    with pytest.raises(BundleValidationError, match="Unexpected archive member"):
        read_bundle(buf)


def test_read_bundle_rejects_excessive_archive_members(monkeypatch):
    monkeypatch.setattr(transfer_bundle_module, "MAX_ARCHIVE_MEMBERS", 1)
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    buf = _build_raw_bundle(manifest, [("data/unlisted.ndjson", b"{}\n")])

    with pytest.raises(BundleValidationError, match="too many archive members"):
        read_bundle(buf)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sha256", "abc", "Invalid sha256"),
        ("count", "1", "Invalid count"),
        ("count", True, "Invalid count"),
        ("count", -1, "Invalid count"),
    ],
)
def test_build_manifest_rejects_invalid_file_summary(
    field: str,
    value: object,
    message: str,
):
    entry: dict[str, Any] = {"path": "data/tasks.ndjson", "type": "task", field: value}

    with pytest.raises(BundleValidationError, match=message):
        build_manifest(
            {"types": ["task"], "preset": "all", "start": None, "end": None},
            [entry],
            "Asia/Shanghai",
        )


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"type": "task"}, "file path is required"),
        ({"path": "data/tasks.ndjson", "type": 2}, "Unsupported file type"),
        ({"path": "data/notes.ndjson", "type": "task"}, "Unexpected path"),
    ],
)
def test_build_manifest_rejects_invalid_file_identity(
    entry: dict[str, Any],
    message: str,
):
    with pytest.raises(BundleValidationError, match=message):
        build_manifest(
            {"types": ["task"], "preset": "all", "start": None, "end": None},
            [entry],
            "Asia/Shanghai",
        )


def test_build_manifest_rejects_duplicate_file_path():
    entry = {"path": "data/tasks.ndjson", "type": "task"}

    with pytest.raises(BundleValidationError, match="Duplicate bundle file path"):
        build_manifest(
            {"types": ["task"], "preset": "all", "start": None, "end": None},
            [entry, entry.copy()],
            "Asia/Shanghai",
        )


@pytest.mark.parametrize("types", ["task", ["unknown"]])
def test_build_manifest_rejects_invalid_selection_types(types: object):
    with pytest.raises(BundleValidationError, match="selection type"):
        build_manifest(
            {"types": types, "preset": "all", "start": None, "end": None},
            [],
            "Asia/Shanghai",
        )


def test_write_bundle_rejects_manifest_record_count_mismatch():
    content  = b'{"_type":"task","_schema":2,"id":"task_1"}\n'
    manifest = _build_single_member_manifest("task", content)

    with pytest.raises(BundleValidationError, match="Count mismatch"):
        write_bundle(io.BytesIO(), manifest, {"task": []})


def test_write_bundle_rejects_manifest_checksum_mismatch():
    record = {"_type": "task", "_schema": 2}
    content = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    manifest                       = _build_single_member_manifest("task", content)
    manifest["files"][0]["sha256"] = "0" * 64

    with pytest.raises(BundleValidationError, match="Checksum mismatch"):
        write_bundle(io.BytesIO(), manifest, {"task": [record]})


def test_write_bundle_replaces_reused_buffer_contents():
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    buf = io.BytesIO(b"stale-prefix-and-tail")
    buf.seek(5)

    write_bundle(buf, manifest, {})

    assert buf.getvalue().startswith(b"PK")
    assert b"stale" not in buf.getvalue()
    assert read_bundle(buf).manifest["format"] == "pendo-bundle"


@pytest.mark.parametrize(
    ("manifest_types", "records", "message"),
    [
        (["task"], {}, "records are missing"),
        ([], {"task": []}, "manifest is missing file entry"),
        ([], {"unknown": []}, "Unsupported type for bundle write"),
    ],
)
def test_write_bundle_rejects_manifest_record_layout_mismatch(
    manifest_types: list[str],
    records: dict[str, list[dict[str, Any]]],
    message: str,
):
    content = b""
    files   = (
        [
            {
                "path": "data/tasks.ndjson",
                "type": "task",
                "count": 0,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        if manifest_types
        else []
    )
    manifest = build_manifest(
        {"types": manifest_types, "preset": "all", "start": None, "end": None},
        files,
        "Asia/Shanghai",
    )

    with pytest.raises(BundleValidationError, match=message):
        write_bundle(io.BytesIO(), manifest, records)


@pytest.mark.parametrize(
    ("limit_name", "message"),
    [
        ("MAX_BUNDLE_RECORDS", "too many records"),
        ("MAX_BUNDLE_MEMBER_BYTES", "maximum file size"),
    ],
)
def test_write_bundle_enforces_per_file_limits(
    monkeypatch,
    limit_name: str,
    message: str,
):
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [{"path": "data/tasks.ndjson", "type": "task"}],
        "Asia/Shanghai",
    )
    monkeypatch.setattr(transfer_bundle_module, limit_name, 0)

    with pytest.raises(BundleValidationError, match=message):
        write_bundle(io.BytesIO(), manifest, {"task": [{"_type": "task", "_schema": 2}]})


@pytest.mark.parametrize(
    ("limit_name", "message"),
    [
        ("MAX_BUNDLE_MANIFEST_BYTES", "manifest exceeds maximum file size"),
        ("MAX_BUNDLE_UNCOMPRESSED_BYTES", "maximum uncompressed size"),
    ],
)
def test_write_bundle_enforces_archive_size_limits(
    monkeypatch,
    limit_name: str,
    message: str,
):
    manifest = build_manifest(
        {"types": [], "preset": "all", "start": None, "end": None},
        [],
        "Asia/Shanghai",
    )
    monkeypatch.setattr(transfer_bundle_module, limit_name, 0)

    with pytest.raises(BundleValidationError, match=message):
        write_bundle(io.BytesIO(), manifest, {})


def test_read_bundle_counts_all_declared_members_in_uncompressed_limit(monkeypatch):
    task_content = b'{"_type":"task","_schema":2}\n'
    note_content = b'{"_type":"note","_schema":2}\n'
    manifest     = build_manifest(
        {"types": ["task", "note"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/tasks.ndjson",
                "type": "task",
                "count": 1,
                "sha256": hashlib.sha256(task_content).hexdigest(),
            },
            {
                "path": "data/notes.ndjson",
                "type": "note",
                "count": 1,
                "sha256": hashlib.sha256(note_content).hexdigest(),
            },
        ],
        "Asia/Shanghai",
    )
    buf = _build_raw_bundle(
        manifest,
        [("data/tasks.ndjson", task_content), ("data/notes.ndjson", note_content)],
    )
    with zipfile.ZipFile(buf, "r") as zf:
        total_size = sum(info.file_size for info in zf.infolist())
    monkeypatch.setattr(
        transfer_bundle_module,
        "MAX_BUNDLE_UNCOMPRESSED_BYTES",
        total_size - 1,
    )

    with pytest.raises(BundleValidationError, match="maximum uncompressed size"):
        read_bundle(buf)


def test_read_bundle_rejects_member_above_uncompressed_limit(monkeypatch):
    monkeypatch.setattr(transfer_bundle_module, "MAX_BUNDLE_MEMBER_BYTES", 16)
    record = {
        "_type": "note",
        "_schema": 2,
        "id": "note_big",
        "title": "大文件",
        "content": "x" * 64,
    }
    content = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    manifest = build_manifest(
        {"types": ["note"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/notes.ndjson",
                "type": "note",
                "count": 1,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/notes.ndjson", content)
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="exceeds maximum file size"):
        read_bundle(buf)


def test_read_bundle_rejects_record_count_above_limit(monkeypatch):
    records = [
        {"_type": "note", "_schema": 2, "id": "note_1", "title": "一"},
        {"_type": "note", "_schema": 2, "id": "note_2", "title": "二"},
    ]
    content = (
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    ).encode("utf-8")
    manifest = build_manifest(
        {"types": ["note"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/notes.ndjson",
                "type": "note",
                "count": 2,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )
    monkeypatch.setattr(transfer_bundle_module, "MAX_BUNDLE_RECORDS", 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/notes.ndjson", content)
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="too many records"):
        read_bundle(buf)


def test_read_bundle_rejects_unknown_file_type():
    manifest = {
        "format": "pendo-bundle",
        "version": 2,
        "bundle_id": "test123",
        "exported_at": "2026-03-29T12:00:00+08:00",
        "source": {"app": "pendo-web", "timezone": "Asia/Shanghai"},
        "selection": {"types": ["unknown"], "preset": "all", "start": None, "end": None},
        "files": [{"path": "data/unknown.ndjson", "type": "unknown", "count": 1, "sha256": "abc"}],
        "attachments_mode": "metadata_only",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/unknown.ndjson", '{"_type":"unknown","_schema":2}\n')
    buf.seek(0)

    with pytest.raises(BundleValidationError, match="unknown"):
        read_bundle(buf)


def test_read_bundle_rejects_legacy_item_fields():
    record = {
        "_type": "event",
        "_schema": 2,
        "id": "event_legacy",
        "title": "旧多节点",
        "start_time": "2026-03-20T09:00:00",
        "milestones": [{"name": "旧节点", "time": "2026-03-20T09:00:00"}],
    }
    content = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    manifest = build_manifest(
        {"types": ["event"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/events.ndjson",
                "type": "event",
                "count": 1,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/events.ndjson", content)
    buf.seek(0)

    parsed = read_bundle(buf)

    assert parsed.records_by_type == {}
    assert parsed.errors[0]["message"] == "Unsupported field for event: milestones"


def test_read_bundle_accepts_tasks_ndjson():
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_1",
                    "title": "导入任务",
                    "content": "正文",
                    "category": "工作",
                    "priority": 3,
                    "status": "open",
                    "created_at": "2026-03-21T09:00:00+08:00",
                    "updated_at": "2026-03-21T09:00:00+08:00",
                }
            ]
        }
    )

    parsed = read_bundle(io.BytesIO(bundle_bytes))

    assert parsed.manifest["format"] == "pendo-bundle"
    assert parsed.records_by_type["task"][0]["title"] == "导入任务"
    assert parsed.errors == []


@pytest.mark.parametrize(
    "preset,payload",
    [
        ("week", {"preset": "week"}),
        ("month", {"preset": "month"}),
        ("quarter", {"preset": "quarter"}),
        ("year", {"preset": "year"}),
        ("last_year", {"preset": "last_year"}),
        ("all", {"preset": "all"}),
        ("custom", {"preset": "custom", "start": "2026-03-01", "end": "2026-03-31"}),
    ],
)
def test_export_preview_accepts_supported_presets(
    client: Any, temp_db: Database, auth_headers: dict, preset: str, payload: dict
):
    _seed_items(temp_db)

    response = client.post(
        "/api/transfer/export/preview",
        headers = auth_headers,
        json    = {"selection": {"types": ["task"], **payload}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["selection"]["preset"] == preset
