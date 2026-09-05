"""Pendo ZIP 传输包的清单、记录序列化与安全读取。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, BinaryIO, Final, cast
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...models.item import Item, get_item_type_value
from ...utils.validators import COMMON_ITEM_FIELDS, SUPPORTED_ITEM_TYPES, TYPE_SPECIFIC_ITEM_FIELDS

TRANSFER_FORMAT: Final                    = "pendo-bundle"
TRANSFER_VERSION: Final                   = 2
MAX_BUNDLE_MANIFEST_BYTES: Final          = 1 * 1024 * 1024
MAX_BUNDLE_MEMBER_BYTES: Final            = 50 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES: Final      = 100 * 1024 * 1024
MAX_BUNDLE_RECORDS: Final                 = 100_000
MAX_IMPORT_RECORDS: Final                 = MAX_BUNDLE_RECORDS * (len(SUPPORTED_ITEM_TYPES) + 1)
SUPPORTED_TYPES: Final[frozenset[str]]    = frozenset(SUPPORTED_ITEM_TYPES)
TYPE_FILE_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "event": "data/events.ndjson",
        "task": "data/tasks.ndjson",
        "ledger": "data/ledger.ndjson",
        "note": "data/notes.ndjson",
        "diary": "data/diary.ndjson",
    }
)
EVENT_COLLECTION_TYPE: Final      = "event_collection"
EVENT_COLLECTION_FILE_NAME: Final = "data/event_collections.ndjson"
# v2 只允许一个清单、五类普通数据文件和一个日程集合文件。
MAX_ARCHIVE_MEMBERS: Final = len(TYPE_FILE_NAMES) + 2
# 宽松导入允许记录省略 ``_type``，此映射从固定文件名恢复类型。
FILE_NAME_TO_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        **{path: item_type for item_type, path in TYPE_FILE_NAMES.items()},
        EVENT_COLLECTION_FILE_NAME: EVENT_COLLECTION_TYPE,
    }
)
TIME_FIELD_BY_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "event": "start_time",
        "task": "plan_date",
        "ledger": "ledger_date",
        "note": "created_at",
        "diary": "diary_date",
    }
)
COMMON_FIELDS: Final[frozenset[str]] = frozenset(COMMON_ITEM_FIELDS) - {
    "type",
    "owner_id",
}
TYPE_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {item_type: frozenset(fields) for item_type, fields in TYPE_SPECIFIC_ITEM_FIELDS.items()}
)
EVENT_COLLECTION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "kind",
        "title",
        "content",
        "category",
        "location",
        "tags",
        "notes",
        "context",
        "visibility",
        "timezone",
        "rrule",
        "reminder_rules",
        "start_time",
        "end_time",
        "source_item_id",
        "created_at",
        "updated_at",
        "deleted",
        "deleted_at",
    }
)
RESERVED_IMPORT_FIELDS: Final[frozenset[str]] = frozenset(
    {"_type", "_schema", "owner_id", "_bundle_line"}
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-fA-F]{64}\Z")


class BundleValidationError(ValueError):
    """传输包结构、清单或记录格式不符合约定。"""


class BundleRecordLimitError(BundleValidationError):
    """传输包内的总记录数超过单次导入上限。"""


@dataclass(slots=True)
class ParsedBundle:
    """已解析的传输包及逐行校验结果。"""

    manifest: dict[str, Any]
    records_by_type: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    event_collections: list[dict[str, Any]] = field(default_factory=list)
    file_summaries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def compute_sha256(bytes_: bytes) -> str:
    """计算传输文件使用的十六进制 SHA-256。"""

    return hashlib.sha256(bytes_).hexdigest()


def build_manifest(
    selection: dict[str, Any], files: list[dict[str, Any]], timezone: str
) -> dict[str, Any]:
    """根据导出选择和文件摘要构建经过校验的 v2 清单。"""

    raw_types = selection.get("types", [])
    if not isinstance(raw_types, list):
        raise BundleValidationError("Bundle selection types must be a list")

    normalized_types: list[str] = []
    for item_type in raw_types:
        # event_collection 是 event 的伴随元数据，不作为独立用户选择项写入清单。
        if item_type == EVENT_COLLECTION_TYPE:
            continue
        if not isinstance(item_type, str) or item_type not in SUPPORTED_TYPES:
            raise BundleValidationError(f"Unsupported selection type: {item_type}")
        if item_type not in normalized_types:
            normalized_types.append(item_type)

    exported_at = selection.get("exported_at") or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    manifest = {
        "format": TRANSFER_FORMAT,
        "version": TRANSFER_VERSION,
        "bundle_id": selection.get("bundle_id") or uuid.uuid4().hex,
        "exported_at": exported_at,
        "source": {"app": "pendo-web", "timezone": timezone},
        "selection": {
            "types": normalized_types,
            "preset": selection.get("preset", "all"),
            "start": selection.get("start"),
            "end": selection.get("end"),
        },
        "files": [entry.copy() for entry in files],
        "attachments_mode": selection.get("attachments_mode", "metadata_only"),
    }
    _validate_manifest(manifest)
    return manifest


def serialize_item(item: Item | dict[str, Any]) -> dict[str, Any]:
    """将条目裁剪为传输格式允许的业务字段。"""

    raw       = item.to_dict() if isinstance(item, Item) else dict(item)
    item_type = get_item_type_value(raw.get("type"))
    if item_type not in SUPPORTED_TYPES:
        raise BundleValidationError(f"Unsupported item type: {item_type}")

    allowed_fields = COMMON_FIELDS | TYPE_FIELDS[item_type]
    record         = {"_type": item_type, "_schema": TRANSFER_VERSION}
    for key, value in raw.items():
        if key in {"type", "owner_id"}:
            continue
        if key in allowed_fields:
            record[key] = value

    context           = record.get("context")
    record["context"] = dict(context) if isinstance(context, dict) else {}
    return record


def serialize_event_collection(collection: dict[str, Any]) -> dict[str, Any]:
    """将日程集合裁剪为独立的传输记录。"""

    record = {"_type": EVENT_COLLECTION_TYPE, "_schema": TRANSFER_VERSION}
    for key, value in dict(collection).items():
        if key == "owner_id":
            continue
        if key in EVENT_COLLECTION_FIELDS:
            record[key] = value
    context           = record.get("context")
    record["context"] = dict(context) if isinstance(context, dict) else {}
    return record


def deserialize_record(record: dict[str, Any]) -> dict[str, Any]:
    """校验普通条目记录，并恢复数据库导入使用的字段结构。"""

    item_type = str(record.get("_type") or "").strip()
    if item_type not in SUPPORTED_TYPES:
        raise BundleValidationError(f"Unsupported record type: {item_type}")

    payload: dict[str, Any] = {"type": item_type}
    allowed_fields          = COMMON_FIELDS | TYPE_FIELDS[item_type]
    for key, value in record.items():
        if key in RESERVED_IMPORT_FIELDS:
            continue
        if key in allowed_fields:
            payload[key] = value
        else:
            raise BundleValidationError(f"Unsupported field for {item_type}: {key}")

    context            = payload.get("context")
    payload["context"] = dict(context) if isinstance(context, dict) else {}
    if "_bundle_line" in record:
        payload["_bundle_line"] = record["_bundle_line"]
    return payload


def deserialize_event_collection_record(record: dict[str, Any]) -> dict[str, Any]:
    """校验日程集合记录，并恢复数据库导入使用的字段结构。"""

    collection: dict[str, Any] = {}
    for key, value in record.items():
        if key in RESERVED_IMPORT_FIELDS:
            continue
        if key in EVENT_COLLECTION_FIELDS:
            collection[key] = value
        else:
            raise BundleValidationError(f"Unsupported field for event_collection: {key}")

    context               = collection.get("context")
    collection["context"] = dict(context) if isinstance(context, dict) else {}
    if "_bundle_line" in record:
        collection["_bundle_line"] = record["_bundle_line"]
    return collection


def _prepare_bundle_members(
    typed_records: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, bytes], dict[str, int]]:
    """把分类型记录编码为规范文件名及 UTF-8 NDJSON。"""

    prepared_members: dict[str, bytes] = {}
    record_counts: dict[str, int]      = {}
    for item_type, records in typed_records.items():
        if item_type == EVENT_COLLECTION_TYPE:
            path = EVENT_COLLECTION_FILE_NAME
        elif item_type in SUPPORTED_TYPES:
            path = TYPE_FILE_NAMES[item_type]
        else:
            raise BundleValidationError(f"Unsupported type for bundle write: {item_type}")
        if len(records) > MAX_BUNDLE_RECORDS:
            raise BundleValidationError(f"{path} has too many records")

        lines = [json.dumps(record, ensure_ascii=False) for record in records]
        payload = ("\n".join(lines) + "\n" if lines else "").encode("utf-8")
        if len(payload) > MAX_BUNDLE_MEMBER_BYTES:
            raise BundleValidationError(f"{path} exceeds maximum file size")
        prepared_members[path] = payload
        record_counts[path]    = len(records)
    return prepared_members, record_counts


def _validate_prepared_members(
    manifest: dict[str, Any],
    prepared_members: dict[str, bytes],
    record_counts: dict[str, int],
) -> None:
    """核对待写数据与清单中的路径、行数和校验和。"""

    manifest_paths: set[str] = set()
    entries                  = cast(list[dict[str, Any]], manifest["files"])
    for entry in entries:
        path, _ = _entry_path_and_type(entry)
        manifest_paths.add(path)
        if path not in prepared_members:
            raise BundleValidationError(f"Bundle records are missing {path}")
        member_payload = prepared_members[path]
        expected_count = cast(int | None, entry.get("count"))
        if expected_count is not None and expected_count != record_counts[path]:
            raise BundleValidationError(f"Count mismatch for {path}")
        expected_sha = cast(str | None, entry.get("sha256"))
        if expected_sha is not None and expected_sha.lower() != compute_sha256(member_payload):
            raise BundleValidationError(f"Checksum mismatch for {path}")

    extra_paths = set(prepared_members) - manifest_paths
    if extra_paths:
        raise BundleValidationError(
            f"Bundle manifest is missing file entry: {sorted(extra_paths)[0]}"
        )


def write_bundle(
    fileobj: BinaryIO,
    manifest: dict[str, Any],
    typed_records: dict[str, list[dict[str, Any]]],
) -> None:
    """写出路径、行数和摘要均与清单一致的规范 ZIP 包。"""

    _validate_manifest(manifest)
    prepared_members, record_counts = _prepare_bundle_members(typed_records)
    _validate_prepared_members(manifest, prepared_members, record_counts)

    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(manifest_bytes) > MAX_BUNDLE_MANIFEST_BYTES:
        raise BundleValidationError("Bundle manifest exceeds maximum file size")
    if len(manifest_bytes) + sum(map(len, prepared_members.values())) > (
        MAX_BUNDLE_UNCOMPRESSED_BYTES
    ):
        raise BundleValidationError("Bundle exceeds maximum uncompressed size")

    # 调用方可能复用缓冲区；先清空，避免旧前缀或尾部数据混入新归档。
    fileobj.seek(0)
    fileobj.truncate(0)
    with ZipFile(fileobj, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        for path, payload in prepared_members.items():
            zf.writestr(path, payload)


def read_bundle(fileobj: BinaryIO) -> ParsedBundle:
    """读取 ``.pendo.zip``，并返回合法记录、逐行错误和宽松模式警告。

    外部工具可省略文件摘要中的 ``sha256``、``count``，也可省略记录中的
    ``_type``、``_schema``；缺失摘要会产生警告，已提供但不匹配的摘要仍会
    作为结构错误拒绝。所有未在清单声明的 ZIP 成员都会被拒绝。
    """

    fileobj.seek(0)
    try:
        with ZipFile(fileobj, "r") as zf:
            infos    = _index_archive_members(zf)
            manifest = _read_manifest(zf, infos)
            _validate_archive_layout(infos, manifest)

            records_by_type: dict[str, list[dict[str, Any]]] = {
                item_type: [] for item_type in TYPE_FILE_NAMES
            }
            event_collections: list[dict[str, Any]] = []
            file_summaries: list[dict[str, Any]]    = []
            errors: list[dict[str, Any]]            = []
            warnings: list[str]                     = []
            total_record_count                      = 0

            entries = cast(list[dict[str, Any]], manifest["files"])
            for entry in entries:
                path, item_type = _entry_path_and_type(entry)
                records, line_count, member_errors, member_warnings = _read_record_member(
                    zf,
                    infos[path],
                    entry,
                    item_type,
                    remaining_records=MAX_IMPORT_RECORDS - total_record_count,
                )
                total_record_count += line_count
                if item_type == EVENT_COLLECTION_TYPE:
                    event_collections.extend(records)
                else:
                    records_by_type[item_type].extend(records)
                errors.extend(member_errors)
                warnings.extend(member_warnings)
                file_summaries.append(
                    {
                        "path": path,
                        "type": item_type,
                        "count": line_count,
                        "valid": len(records),
                    }
                )

            return ParsedBundle(
                manifest        = manifest,
                records_by_type = {
                    item_type: records for item_type, records in records_by_type.items() if records
                },
                event_collections = event_collections,
                file_summaries    = file_summaries,
                errors            = errors,
                warnings          = warnings,
            )
    except BundleValidationError:
        raise
    except (BadZipFile, EOFError, NotImplementedError, OSError, RuntimeError) as exc:
        raise BundleValidationError("Invalid bundle zip") from exc


def _index_archive_members(zf: ZipFile) -> dict[str, ZipInfo]:
    """建立不丢失重复项的成员索引，并拒绝加密或异常成员数量。"""

    members = zf.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise BundleValidationError("Bundle contains too many archive members")

    infos: dict[str, ZipInfo] = {}
    for info in members:
        if info.filename in infos:
            raise BundleValidationError(f"Duplicate archive member: {info.filename}")
        if info.flag_bits & 0x1:
            raise BundleValidationError(
                f"Encrypted archive member is not supported: {info.filename}"
            )
        infos[info.filename] = info
    return infos


def _read_member_bytes(zf: ZipFile, info: ZipInfo, *, max_bytes: int, label: str) -> bytes:
    """在读取前后同时执行成员大小边界检查。"""

    if info.file_size > max_bytes:
        raise BundleValidationError(f"{label} exceeds maximum file size")
    try:
        payload = zf.read(info)
    except (BadZipFile, EOFError, NotImplementedError, OSError, RuntimeError) as exc:
        raise BundleValidationError(f"Cannot read archive member: {info.filename}") from exc
    if len(payload) > max_bytes:
        raise BundleValidationError(f"{label} exceeds maximum file size")
    return payload


def _read_manifest(zf: ZipFile, infos: dict[str, ZipInfo]) -> dict[str, Any]:
    """读取并校验清单根对象。"""

    info = infos.get("manifest.json")
    if info is None:
        raise BundleValidationError("Bundle is missing manifest.json")
    payload = _read_member_bytes(
        zf,
        info,
        max_bytes = MAX_BUNDLE_MANIFEST_BYTES,
        label     = "Bundle manifest",
    )
    try:
        manifest: object = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleValidationError("Bundle manifest is not valid UTF-8 JSON") from exc
    _validate_manifest(manifest)
    return cast(dict[str, Any], manifest)


def _entry_path_and_type(entry: dict[str, Any]) -> tuple[str, str]:
    """校验文件条目的规范路径，并恢复可省略的记录类型。"""

    path = entry.get("path")
    if not isinstance(path, str) or not path:
        raise BundleValidationError("Bundle manifest file path is required")

    declared_type = entry.get("type")
    if declared_type is None:
        item_type = FILE_NAME_TO_TYPE.get(path)
    elif isinstance(declared_type, str):
        item_type = declared_type
    else:
        item_type = None
    if item_type not in SUPPORTED_TYPES and item_type != EVENT_COLLECTION_TYPE:
        raise BundleValidationError(f"Unsupported file type in manifest: {declared_type}")

    expected_path = (
        EVENT_COLLECTION_FILE_NAME
        if item_type == EVENT_COLLECTION_TYPE
        else TYPE_FILE_NAMES[item_type]
    )
    if path != expected_path:
        raise BundleValidationError(f"Unexpected path for {item_type}: {path}")
    return path, item_type


def _validate_manifest(manifest: object) -> None:
    """校验清单根结构，并分派各语义区块的精确校验。"""

    if not isinstance(manifest, dict):
        raise BundleValidationError("Bundle manifest must be a JSON object")
    if manifest.get("format") != TRANSFER_FORMAT:
        raise BundleValidationError("Unsupported bundle format")
    version = manifest.get("version")
    if type(version) is not int or version != TRANSFER_VERSION:
        raise BundleValidationError("Unsupported bundle version")
    _validate_manifest_metadata(manifest)
    _validate_manifest_selection(manifest.get("selection"))
    _validate_manifest_source(manifest.get("source"))
    _validate_manifest_files(manifest.get("files"))


def _validate_manifest_metadata(manifest: dict[Any, Any]) -> None:
    """校验清单标识、导出时间和附件模式等元数据。"""

    bundle_id = manifest.get("bundle_id")
    if bundle_id is not None and (
        not isinstance(bundle_id, str) or not bundle_id.strip() or len(bundle_id) > 256
    ):
        raise BundleValidationError("Bundle id must be a non-empty string")
    exported_at = manifest.get("exported_at")
    if exported_at is not None and (
        not isinstance(exported_at, str) or not exported_at.strip() or len(exported_at) > 128
    ):
        raise BundleValidationError("Bundle exported_at must be a non-empty string")
    attachments_mode = manifest.get("attachments_mode", "metadata_only")
    if attachments_mode != "metadata_only":
        raise BundleValidationError(f"Unsupported attachments mode: {attachments_mode}")


def _validate_manifest_selection(selection: object) -> None:
    """校验导出选择中的类型列表。"""

    if not isinstance(selection, dict):
        raise BundleValidationError("Bundle manifest selection must be an object")
    selection_types = selection.get("types")
    if not isinstance(selection_types, list):
        raise BundleValidationError("Bundle selection types must be a list")
    for item_type in selection_types:
        if not isinstance(item_type, str) or item_type not in SUPPORTED_TYPES:
            raise BundleValidationError(f"Unsupported selection type: {item_type}")


def _validate_manifest_source(source: object) -> None:
    """校验来源对象及其 IANA 时区。"""

    if not isinstance(source, dict):
        raise BundleValidationError("Bundle manifest source must be an object")
    timezone_name = source.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise BundleValidationError("Bundle manifest source timezone is required")
    try:
        ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise BundleValidationError(f"Invalid source timezone: {timezone_name}") from exc


def _validate_manifest_files(files: object) -> None:
    """校验文件条目的路径、去重、行数和 SHA-256 摘要。"""

    if not isinstance(files, list):
        raise BundleValidationError("Bundle manifest files must be a list")
    seen_paths: set[str] = set()
    for raw_entry in files:
        if not isinstance(raw_entry, dict):
            raise BundleValidationError("Bundle manifest files must contain objects")
        entry = cast(dict[str, Any], raw_entry)
        path, _ = _entry_path_and_type(entry)
        if path in seen_paths:
            raise BundleValidationError(f"Duplicate bundle file path: {path}")
        seen_paths.add(path)

        expected_count = entry.get("count")
        if expected_count is not None:
            if type(expected_count) is not int or expected_count < 0:
                raise BundleValidationError(f"Invalid count for {path}")
            if expected_count > MAX_BUNDLE_RECORDS:
                raise BundleValidationError(f"{path} has too many records")

        expected_sha = entry.get("sha256")
        if expected_sha is not None and (
            not isinstance(expected_sha, str) or _SHA256_RE.fullmatch(expected_sha) is None
        ):
            raise BundleValidationError(f"Invalid sha256 for {path}")


def _validate_archive_layout(infos: dict[str, ZipInfo], manifest: dict[str, Any]) -> None:
    """核对清单与 ZIP 成员一一对应，并累计未压缩大小和声明行数。"""

    expected_paths        = {"manifest.json"}
    total_size            = infos["manifest.json"].file_size
    declared_record_count = 0
    entries               = cast(list[dict[str, Any]], manifest["files"])
    for entry in entries:
        path, _ = _entry_path_and_type(entry)
        expected_paths.add(path)
        info = infos.get(path)
        if info is None:
            raise BundleValidationError(f"Bundle is missing {path}")
        if info.file_size > MAX_BUNDLE_MEMBER_BYTES:
            raise BundleValidationError(f"{path} exceeds maximum file size")
        total_size += info.file_size
        expected_count = entry.get("count")
        if expected_count is not None:
            declared_record_count += cast(int, expected_count)

    unexpected_paths = set(infos) - expected_paths
    if unexpected_paths:
        raise BundleValidationError(f"Unexpected archive member: {sorted(unexpected_paths)[0]}")
    if total_size > MAX_BUNDLE_UNCOMPRESSED_BYTES:
        raise BundleValidationError("Bundle exceeds maximum uncompressed size")
    if declared_record_count > MAX_IMPORT_RECORDS:
        raise BundleRecordLimitError("Bundle contains too many total records")


def _parse_record_line(line: str, *, item_type: str, path: str, line_number: int) -> dict[str, Any]:
    """解析单行 NDJSON，并补齐允许省略的类型与格式版本。"""

    raw_record: object = json.loads(line)
    if not isinstance(raw_record, dict):
        raise BundleValidationError(f"Record in {path}:{line_number} must be a JSON object")
    record = cast(dict[str, Any], raw_record)

    record_type = record.get("_type")
    if record_type is None:
        record["_type"] = item_type
    elif record_type != item_type:
        raise BundleValidationError(f"Record type mismatch in {path}:{line_number}")

    record_schema = record.get("_schema")
    if record_schema is None:
        record["_schema"] = TRANSFER_VERSION
    elif type(record_schema) is not int or record_schema != TRANSFER_VERSION:
        raise BundleValidationError(f"Unsupported schema in {path}:{line_number}")

    record["_bundle_line"] = line_number
    if item_type == EVENT_COLLECTION_TYPE:
        return deserialize_event_collection_record(record)
    return deserialize_record(record)


def _read_record_member(
    zf: ZipFile,
    info: ZipInfo,
    entry: dict[str, Any],
    item_type: str,
    *,
    remaining_records: int,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]], list[str]]:
    """读取一个 NDJSON 成员，保留合法行并收集可定位的逐行错误。"""

    path       = info.filename
    file_bytes = _read_member_bytes(
        zf,
        info,
        max_bytes = MAX_BUNDLE_MEMBER_BYTES,
        label     = path,
    )
    warnings: list[str] = []
    expected_sha        = entry.get("sha256")
    if expected_sha is None:
        warnings.append(f"{path}: 缺少 sha256 校验和，跳过完整性检查")
    elif cast(str, expected_sha).lower() != compute_sha256(file_bytes):
        raise BundleValidationError(f"Checksum mismatch for {path}")

    try:
        file_lines = file_bytes.decode("utf-8").split("\n")
    except UnicodeDecodeError as exc:
        raise BundleValidationError(f"{path} is not valid UTF-8") from exc

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]]  = []
    line_count                    = 0
    for line_number, line in enumerate(file_lines, start=1):
        if not line.strip():
            continue
        line_count += 1
        if line_count > remaining_records:
            raise BundleRecordLimitError("Bundle contains too many total records")
        try:
            records.append(
                _parse_record_line(
                    line,
                    item_type   = item_type,
                    path        = path,
                    line_number = line_number,
                )
            )
        except ValueError as exc:
            errors.append(
                {
                    "path": path,
                    "line": line_number,
                    "type": item_type,
                    "message": str(exc),
                }
            )

    expected_count = entry.get("count")
    if expected_count is None:
        warnings.append(f"{path}: 缺少 count 字段，跳过行数校验")
    elif line_count != expected_count:
        raise BundleValidationError(f"Count mismatch for {path}")
    return records, line_count, errors, warnings
