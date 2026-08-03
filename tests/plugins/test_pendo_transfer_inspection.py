"""导入检查和选项验证。"""

from __future__ import annotations

import tests.helpers.pendo_web_transfer_test_support as _fixture_support
from tests.helpers.pendo_web_transfer_test_support import (
    Any,
    Database,
    _build_sample_bundle_bytes,
    build_manifest,
    hashlib,
    io,
    json,
    pytest,
    transfer_api,
    zipfile,
)

auth_headers = _fixture_support.auth_headers


def test_bundle_inspection_hides_low_level_validation_details() -> None:
    """损坏压缩包只返回稳定公开错误，不暴露底层 ZIP 解析文本。"""

    with pytest.raises(transfer_api.HTTPException) as exc_info:
        transfer_api._inspect_bundle_data(b"not-a-zip")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "导入包格式或内容校验失败"


def test_execute_import_rejects_oversized_options_header(
    client: Any, auth_headers: dict[str, str]
) -> None:
    """导入选项头在 JSON 解析前受长度限制。"""

    response = client.post(
        "/api/transfer/import/execute",
        headers={**auth_headers, "X-Transfer-Options": "x" * 4097},
        content=b"unused",
    )

    assert response.status_code == 422


def test_import_inspect_returns_summary_and_row_errors(client: Any, auth_headers: dict):
    valid_task = {
        "_type": "task",
        "_schema": 2,
        "id": "task_bundle_1",
        "title": "导入任务",
        "content": "来自备份",
        "category": "工作",
        "priority": 3,
        "status": "open",
        "created_at": "2026-03-20T09:00:00+08:00",
        "updated_at": "2026-03-20T09:00:00+08:00",
    }
    invalid_task = {"_type": "task", "_schema": 2, "id": "bad"}
    records = [valid_task, invalid_task]
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records).encode("utf-8")
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/tasks.ndjson",
                "type": "task",
                "count": len(records),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/tasks.ndjson", content)
    bundle_bytes = buf.getvalue()

    response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=bundle_bytes,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["summary"]["types"] == ["task"]
    assert body["counts"]["valid"] == 1
    assert body["counts"]["errors"] == 1
    assert "bundle_id" in body
    assert body["already_imported"] is False
    assert body["samples"][0]["title"] == "导入任务"
    assert body["errors"][0]["path"] == "data/tasks.ndjson"
    assert body["errors"][0]["line"] == 2


def test_import_inspect_detects_already_imported_bundle(
    client: Any, temp_db: Database, auth_headers: dict
):
    bundle_bytes = _build_sample_bundle_bytes(
        {
            "task": [
                {
                    "_type": "task",
                    "_schema": 2,
                    "id": "task_idem_1",
                    "title": "幂等测试",
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

    # 先执行一次导入
    client.post(
        "/api/transfer/import/execute",
        headers={
            **auth_headers,
            "X-Transfer-Options": json.dumps({"types": ["task"], "conflict_policy": "skip"}),
        },
        content=bundle_bytes,
    )

    # 再次预检同一个 bundle
    response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=bundle_bytes,
    )
    assert response.status_code == 200
    assert response.json()["data"]["already_imported"] is True


def test_import_inspect_preserves_original_line_number_for_normalization_errors(
    client: Any, auth_headers: dict
):
    rows = [
        '{"_type":"task","_schema":2,"id":"broken-json"',
        json.dumps(
            {
                "_type": "task",
                "_schema": 2,
                "id": "task_bad_line",
                "created_at": "2026-03-20T09:00:00+08:00",
                "updated_at": "2026-03-20T09:00:00+08:00",
            },
            ensure_ascii=False,
        ),
    ]
    content = ("\n".join(rows) + "\n").encode("utf-8")
    manifest = build_manifest(
        {"types": ["task"], "preset": "all", "start": None, "end": None},
        [
            {
                "path": "data/tasks.ndjson",
                "type": "task",
                "count": 2,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "Asia/Shanghai",
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/tasks.ndjson", content)

    response = client.post(
        "/api/transfer/import/inspect",
        headers=auth_headers,
        content=buf.getvalue(),
    )

    assert response.status_code == 200
    errors = response.json()["data"]["errors"]
    assert errors[0]["line"] == 1
    assert errors[1]["line"] == 2
