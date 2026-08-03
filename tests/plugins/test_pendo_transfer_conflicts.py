"""导入时区、冲突和分页。"""

from __future__ import annotations

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from tests.helpers.pendo_web_transfer_test_support import (
    OWNER_ID,
    ROOT,
    Any,
    Database,
    DuplicateBundleImportError,
    SimpleNamespace,
    ZoneInfo,
    _build_sample_bundle_bytes,
    _simple_task_bundle,
    datetime,
    io,
    json,
    pytest,
    read_bundle,
    timezone,
    transfer_api,
)

auth_headers = _fixture_support.auth_headers


def test_transfer_logs_endpoint(client: Any, temp_db: Database, auth_headers: dict):
    temp_db.log_transfer(
        owner_id=OWNER_ID,
        action="export",
        filename="test.pendo.zip",
        types=["task"],
        record_count=5,
        result_summary={"counts": {"task": 5}},
    )

    response = client.get("/api/transfer/logs", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()["data"]
    assert len(body["logs"]) >= 1
    assert body["logs"][0]["action"] == "export"


def test_import_source_timezone_normalizes_naive_and_aware_datetimes_to_utc():
    from plugins.pendo.web.services.bundle_import import inspect_bundle_bytes

    bundle = _build_sample_bundle_bytes(
        {
            "event": [
                {
                    "_type": "event",
                    "_schema": 2,
                    "id": "tz-event",
                    "title": "DST transition",
                    "start_time": "2026-03-08T01:30:00",
                    "end_time": "2026-03-08T03:30:00",
                    "created_at": "2026-03-07T20:00:00-08:00",
                    "updated_at": "2026-03-08T04:00:00",
                }
            ]
        },
        timezone_name="America/New_York",
    )

    _parsed, records, errors = inspect_bundle_bytes(bundle)

    assert errors == []
    assert records[0]["start_time"] == "2026-03-08T06:30:00+00:00"
    assert records[0]["end_time"] == "2026-03-08T07:30:00+00:00"
    assert records[0]["created_at"] == "2026-03-08T04:00:00+00:00"
    assert records[0]["updated_at"] == "2026-03-08T08:00:00+00:00"


@pytest.mark.parametrize(
    "local_time,expected_message",
    [
        ("2026-03-08T02:30:00", "Nonexistent local time"),
        ("2026-11-01T01:30:00", "Ambiguous local time"),
    ],
)
def test_import_source_timezone_rejects_unrepresentable_dst_wall_times(
    local_time: str,
    expected_message: str,
):
    from plugins.pendo.web.services.bundle_import import inspect_bundle_bytes

    bundle = _build_sample_bundle_bytes(
        {
            "event": [
                {
                    "_type": "event",
                    "_schema": 2,
                    "id": "bad-dst",
                    "title": "bad DST",
                    "start_time": local_time,
                }
            ]
        },
        timezone_name="America/New_York",
    )

    _parsed, records, errors = inspect_bundle_bytes(bundle)

    assert records == []
    assert len(errors) == 1
    assert expected_message in errors[0]["message"]


def test_import_missing_timestamps_use_utc_clock_not_host_local_time():
    from plugins.pendo.web.services.bundle_import import normalize_import_payload

    fixed = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    normalized = normalize_import_payload(
        {"type": "task", "title": "done", "status": "done"},
        source_zone=ZoneInfo("Pacific/Auckland"),
        now=fixed,
    )

    assert normalized["created_at"] == "2030-01-01T12:00:00+00:00"
    assert normalized["updated_at"] == "2030-01-01T12:00:00+00:00"
    assert normalized["completed_at"] == "2030-01-01T12:00:00+00:00"


def test_all_import_endpoints_reject_total_record_limit_across_files(
    client: Any,
    temp_db: Database,
    auth_headers: dict,
):
    tasks = [{"_type": "task", "_schema": 2} for _ in range(10_001)]
    notes = [{"_type": "note", "_schema": 2} for _ in range(10_000)]
    bundle = _build_sample_bundle_bytes({"task": tasks, "note": notes})
    before = temp_db.get_connection().execute("SELECT COUNT(*) FROM items").fetchone()[0]

    requests = [
        ("/api/transfer/import/inspect", {}),
        ("/api/transfer/import/samples", {}),
        (
            "/api/transfer/import/execute",
            {"X-Transfer-Options": json.dumps({"conflict_policy": "skip"})},
        ),
    ]
    for path, extra_headers in requests:
        response = client.post(
            path,
            headers={**auth_headers, **extra_headers},
            content=bundle,
        )
        assert response.status_code == 413, (path, response.text)
    after = temp_db.get_connection().execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert after == before


def test_total_record_limit_accepts_exact_boundary_across_files():
    tasks = [{"_type": "task", "_schema": 2} for _ in range(10_000)]
    notes = [{"_type": "note", "_schema": 2} for _ in range(10_000)]
    bundle = _build_sample_bundle_bytes({"task": tasks, "note": notes})

    parsed = read_bundle(io.BytesIO(bundle))

    assert sum(summary["count"] for summary in parsed.file_summaries) == 20_000


def test_import_conflict_policies_have_distinct_persistent_results(
    client: Any,
    temp_db: Database,
    auth_headers: dict,
):
    def import_source(title: str, policy: str):
        bundle = _build_sample_bundle_bytes(
            {
                "task": [
                    {
                        "_type": "task",
                        "_schema": 2,
                        "id": "policy-source",
                        "title": title,
                        "status": "open",
                    }
                ]
            }
        )
        return client.post(
            "/api/transfer/import/execute",
            headers={
                **auth_headers,
                "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": policy}),
            },
            content=bundle,
        )

    first = import_source("first", "skip")
    skipped = import_source("must not replace", "skip")
    overwritten = import_source("overwritten", "overwrite")
    duplicated = import_source("duplicate", "duplicate")
    isolated = import_source("isolated", "isolate")

    assert first.json()["data"]["results"] == {
        "inserted": 1,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert skipped.json()["data"]["results"]["skipped"] == 1
    assert overwritten.json()["data"]["results"]["updated"] == 1
    assert duplicated.json()["data"]["results"]["inserted"] == 1
    assert isolated.json()["data"]["results"]["inserted"] == 1

    imported = [
        item
        for item in temp_db.get_items(OWNER_ID, filters={"type": "task"}, limit=20)
        if item.context.get("import", {}).get("source_id") == "policy-source"
    ]
    assert sorted(item.title for item in imported) == ["duplicate", "isolated", "overwritten"]
    by_title = {item.title: item for item in imported}
    assert by_title["overwritten"].context["import"]["policy"] == "overwrite"
    assert by_title["duplicate"].context["import"]["policy"] == "duplicate"
    assert by_title["isolated"].context["import"]["policy"] == "isolate"
    assert by_title["isolated"].context["import"]["namespace"]


def test_event_collection_overwrite_reuses_imported_collection_and_event_ids(
    client: Any,
    temp_db: Database,
    auth_headers: dict,
):
    def bundle(collection_title: str, event_title: str) -> bytes:
        return _build_sample_bundle_bytes(
            {
                "event_collection": [
                    {
                        "_type": "event_collection",
                        "_schema": 2,
                        "id": "source-collection",
                        "kind": "multi_node",
                        "title": collection_title,
                    }
                ],
                "event": [
                    {
                        "_type": "event",
                        "_schema": 2,
                        "id": "source-event",
                        "title": event_title,
                        "start_time": "2030-01-01T09:00:00",
                        "event_collection_id": "source-collection",
                    }
                ],
            },
            selection={"types": ["event"], "preset": "all", "start": None, "end": None},
        )

    first = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["event"], "conflict_policy": "skip"}),
        },
        content=bundle("collection v1", "event v1"),
    )
    second = client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["event"], "conflict_policy": "overwrite"}),
        },
        content=bundle("collection v2", "event v2"),
    )

    assert first.status_code == 200
    assert first.json()["data"]["results"]["inserted"] == 2
    assert second.status_code == 200
    assert second.json()["data"]["results"]["updated"] == 2
    collections = (
        temp_db.get_connection()
        .execute(
            "SELECT id, title FROM event_collections WHERE owner_id = ? AND deleted = 0",
            (OWNER_ID,),
        )
        .fetchall()
    )
    events = temp_db.get_items(OWNER_ID, filters={"type": "event"}, limit=20)
    assert [(row["title"]) for row in collections] == ["collection v2"]
    assert [(event.title) for event in events] == ["event v2"]
    assert events[0].event_collection_id == collections[0]["id"]


def test_item_identity_generation_and_source_index_ignore_unsafe_metadata(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部 ID 全局查重；来源索引忽略隔离、损坏或无来源的上下文。"""

    assert transfer_api._get_item_identity(temp_db, None) is None
    collision_id = "a" * 32
    task_contexts = {
        collision_id: {"import": {"source_id": "source-valid", "policy": "skip"}},
        "item-isolated": {"import": {"source_id": "source-isolated", "policy": "isolate"}},
        "item-no-source": {"import": {"policy": "skip"}},
        "item-bad-import": {"import": "not-an-object"},
    }
    for item_id, context in task_contexts.items():
        temp_db.insert_item(
            {
                "id": item_id,
                "owner_id": OWNER_ID,
                "type": "task",
                "title": item_id,
                "status": "open",
                "context": context,
            }
        )

    identity = transfer_api._get_item_identity(temp_db, collision_id)
    assert identity == {
        "id": collision_id,
        "owner_id": OWNER_ID,
        "type": "task",
        "deleted": 0,
    }
    assert transfer_api._decode_import_context({"key": "value"}) == {"key": "value"}
    assert transfer_api._decode_import_context("{broken") == {}
    assert transfer_api._index_imported_item_sources(temp_db, OWNER_ID, set()) == {}
    assert transfer_api._index_imported_item_sources(temp_db, OWNER_ID, {"task"}) == {
        ("task", "source-valid"): collision_id
    }

    generated = iter([SimpleNamespace(hex=collision_id), SimpleNamespace(hex="b" * 32)])
    monkeypatch.setattr(transfer_api.uuid, "uuid4", lambda: next(generated))
    assert transfer_api._new_import_item_id(temp_db) == "b" * 32


def test_collection_identity_generation_and_source_index_ignore_unsafe_metadata(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """集合 ID 也做全局查重，且只索引可覆盖的非隔离来源。"""

    assert transfer_api._get_event_collection_identity(temp_db, None) is None
    collision_id = "c" * 16
    collection_contexts = {
        collision_id: {"import": {"source_id": "collection-source", "policy": "skip"}},
        "collection-isolated": {
            "import": {"source_id": "collection-isolated-source", "policy": "isolate"}
        },
        "collection-no-source": {"import": {"policy": "skip"}},
        "collection-bad-import": {"import": "not-an-object"},
    }
    for collection_id, context in collection_contexts.items():
        temp_db.create_event_collection(
            {
                "id": collection_id,
                "owner_id": OWNER_ID,
                "kind": "multi_node",
                "title": collection_id,
                "context": context,
            }
        )

    identity = transfer_api._get_event_collection_identity(temp_db, collision_id)
    assert identity == {
        "id": collision_id,
        "owner_id": OWNER_ID,
        "kind": "multi_node",
        "deleted": 0,
    }
    assert transfer_api._index_imported_collection_sources(temp_db, OWNER_ID) == {
        "collection-source": collision_id
    }

    generated = iter(
        [
            SimpleNamespace(hex=collision_id + "0" * 16),
            SimpleNamespace(hex="d" * 32),
        ]
    )
    monkeypatch.setattr(transfer_api.uuid, "uuid4", lambda: next(generated))
    assert transfer_api._new_import_collection_id(temp_db) == "d" * 16


def test_collection_import_planner_skips_existing_source_and_rejects_duplicates(
    temp_db: Database,
) -> None:
    """同来源 skip 复用内部 ID，同一个 bundle 内的重复来源则立即失败。"""

    temp_db.create_event_collection(
        {
            "id": "existing-collection",
            "owner_id": OWNER_ID,
            "kind": "multi_node",
            "title": "现有集合",
            "context": {"import": {"source_id": "source-collection", "policy": "skip"}},
        }
    )
    operations, id_map, decisions = transfer_api._prepare_collection_import_operations(
        temp_db,
        OWNER_ID,
        [{"id": "source-collection", "kind": "multi_node", "title": "待跳过"}],
        selected_types={"event"},
        conflict_policy="skip",
        bundle_id="bundle-skip",
    )

    assert operations == []
    assert id_map == {"source-collection": "existing-collection"}
    assert decisions[0][0] == "skipped"

    duplicate = {"id": "duplicate-source", "kind": "multi_node", "title": "重复"}
    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api._prepare_collection_import_operations(
            temp_db,
            OWNER_ID,
            [duplicate, duplicate],
            selected_types={"event"},
            conflict_policy="duplicate",
            bundle_id="bundle-duplicate",
        )
    assert exc_info.value.status_code == 422


def test_relationship_helpers_drop_unresolved_or_malformed_external_links() -> None:
    """只重写同批次关系，异常结构和未解析外部 ID 都不能进入内部字段。"""

    item_id_map = {"source": "internal"}
    assert transfer_api._result_entry({"type": "task", "id": "one"}) == {
        "type": "task",
        "id": "one",
        "title": "无标题",
    }
    assert transfer_api._remap_note_references("invalid", item_id_map) == []
    assert transfer_api._remap_note_references(
        [None, {"kind": "item", "id": "source"}, {"kind": "item", "id": "missing"}],
        item_id_map,
    ) == [{"kind": "item", "id": "internal"}]
    assert transfer_api._remap_related_item_ids(None, item_id_map) == []

    untouched = {"type": "task", "title": "无关系"}
    assert transfer_api._rewrite_import_item_relationships(untouched, {}) is untouched
    assert transfer_api._rewrite_import_item_relationships(
        {"type": "note", "related_items": ["source"]}, item_id_map
    )["related_items"] == ["internal"]
    assert transfer_api._rewrite_import_item_relationships(
        {"type": "note", "references": [{"id": "source"}]}, item_id_map
    )["references"] == [{"id": "internal"}]

    external_source = {"type": "event", "source_item_id": "outside"}
    rewritten = transfer_api._rewrite_import_item_relationships(external_source, item_id_map)
    assert "source_item_id" not in rewritten

    no_collection = {"type": "event"}
    transfer_api._rewrite_event_collection_reference(no_collection, {})
    assert "event_collection_id" not in no_collection
    missing_collection = {"type": "event", "event_collection_id": "outside"}
    transfer_api._rewrite_event_collection_reference(missing_collection, {})
    assert "event_collection_id" not in missing_collection


def test_import_validation_aborts_on_invalid_rows_and_rejects_duplicate_source_ids(
    temp_db: Database,
) -> None:
    """默认无效策略保持全包中止，规划阶段也拒绝重复外部身份。"""

    parsed, valid_records, _errors = transfer_api._inspect_bundle_data(
        _simple_task_bundle("source-one")
    )
    with pytest.raises(transfer_api.HTTPException) as invalid_error:
        transfer_api._validate_import_request(
            parsed=parsed,
            errors=[{"message": "invalid row"}],
            parsed_options={},
            owner_id=OWNER_ID,
            db=temp_db,
        )
    assert invalid_error.value.status_code == 422

    duplicate_records = [dict(valid_records[0]), dict(valid_records[0])]
    with pytest.raises(transfer_api.HTTPException) as duplicate_error:
        transfer_api._plan_item_imports(
            db=temp_db,
            owner_id=OWNER_ID,
            valid_records=duplicate_records,
            selected_types={"task"},
            conflict_policy="isolate",
        )
    assert duplicate_error.value.status_code == 422


def test_import_commit_maps_duplicate_bundle_and_unique_constraint_errors(
    temp_db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务层竞争统一映射为 409，且不把 SQLite 原始错误暴露给调用者。"""

    def raise_duplicate_bundle(**_kwargs: Any) -> None:
        raise DuplicateBundleImportError("bundle-race")

    monkeypatch.setattr(temp_db, "execute_import_bundle", raise_duplicate_bundle)
    with pytest.raises(transfer_api.HTTPException) as bundle_error:
        transfer_api._commit_import_plan(
            db=temp_db,
            owner_id=OWNER_ID,
            bundle_id="bundle-race",
            operations=[],
            collection_operations=[],
            filename=None,
            selected_types={"task"},
            results={"inserted": 0, "updated": 0, "skipped": 0, "failed": 0},
            force=False,
        )
    assert bundle_error.value.status_code == 409

    def raise_unique_constraint(**_kwargs: Any) -> None:
        cause = transfer_api.sqlite3.IntegrityError("private sqlite detail")
        cause.sqlite_errorcode = transfer_api.sqlite3.SQLITE_CONSTRAINT_UNIQUE
        raise RuntimeError("wrapped storage failure") from cause

    monkeypatch.setattr(temp_db, "execute_import_bundle", raise_unique_constraint)
    with pytest.raises(transfer_api.HTTPException) as unique_error:
        transfer_api._commit_import_plan(
            db=temp_db,
            owner_id=OWNER_ID,
            bundle_id=None,
            operations=[],
            collection_operations=[],
            filename=None,
            selected_types={"task"},
            results={"inserted": 0, "updated": 0, "skipped": 0, "failed": 0},
            force=False,
        )
    assert unique_error.value.status_code == 409
    assert "private sqlite detail" not in str(unique_error.value.detail)


def test_unique_constraint_detection_has_a_bounded_exception_chain() -> None:
    """异常链遍历设硬上限，防止恶意或损坏链条导致无界工作。"""

    error: BaseException = RuntimeError("root")
    for index in range(10):
        wrapper = RuntimeError(f"wrapper-{index}")
        wrapper.__cause__ = error
        error = wrapper

    assert transfer_api._is_unique_constraint_failure(error) is False


def test_import_samples_invalid_pagination_headers_fall_back_to_defaults(
    client: Any, auth_headers: dict[str, str]
) -> None:
    """旧客户端的坏分页头不会导致 500，而是回退到安全默认值。"""

    response = client.post(
        "/api/transfer/import/samples",
        headers={
            **auth_headers,
            "X-Transfer-Page": "invalid",
            "X-Transfer-Page-Size": "invalid",
        },
        content=_simple_task_bundle("sample-defaults"),
    )

    assert response.status_code == 200
    assert response.json()["data"]["page"] == 1
    assert response.json()["data"]["page_size"] == 20


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (1, -1)])
def test_transfer_logs_reject_invalid_pagination_even_when_called_directly(
    temp_db: Database, limit: int, offset: int
) -> None:
    """直接调用也执行与 FastAPI 查询约束一致的边界检查。"""

    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api.get_transfer_logs(
            owner_id=OWNER_ID,
            db=temp_db,
            limit=limit,
            offset=offset,
        )
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_transfer_logs_http_query_constraints_return_422(
    client: Any, auth_headers: dict[str, str], query: str
) -> None:
    """HTTP 层在进入数据库前拒绝越界日志分页。"""

    response = client.get(f"/api/transfer/logs?{query}", headers=auth_headers)
    assert response.status_code == 422


def test_query_items_for_types_paginates_full_export():
    class FakeDB:
        def __init__(self):
            self.calls = []

        def get_items(self, owner_id, filters=None, limit=100, offset=0, *, use_cache=True):
            assert use_cache is False
            self.calls.append((owner_id, filters, limit, offset))
            total = 2505
            if offset >= total:
                return []
            remaining = total - offset
            size = min(limit, remaining)
            return [{"id": f"task_{offset + idx}"} for idx in range(size)]

    db = FakeDB()
    result = transfer_api.query_items_for_types(db, OWNER_ID, ["task"])

    assert len(result["task"]) == 2505
    assert db.calls[:3] == [
        (OWNER_ID, {"type": "task"}, 1000, 0),
        (OWNER_ID, {"type": "task"}, 1000, 1000),
        (OWNER_ID, {"type": "task"}, 1000, 2000),
    ]


def test_transfer_page_sources_register_route_and_header_entry():
    app_src = (ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    transfer_src = (
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "transfer.js"
    ).read_text(encoding="utf-8")
    header_src = (
        ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "components" / "header.js"
    ).read_text(encoding="utf-8")

    assert "registerRoute('transfer'" in app_src
    assert "export function render(container)" in transfer_src
    assert "transfer: '数据迁移'" in header_src
