"""发现、验证并归档 Codex 任务生成的图片制品。"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from core.image_validation import (
    ImageValidationError,
    ImageValidationLimits,
    stat_identity,
    validate_image_fd,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\n]+)\)")
IMAGE_LINE_RE = re.compile(r"(?im)^\s*(?:图片|image|img|artifact)\s*[:：]\s*(.+?)\s*$")
COPY_CHUNK_BYTES = 1024 * 1024
MAX_REFERENCED_PATH_CHARS = 32_768


@dataclass(frozen=True)
class CodexImageArtifact:
    path: str
    absolute_path: str
    source: str
    original_path: str

    def as_record(self) -> dict[str, str]:
        return {
            "path": self.path,
            "absolute_path": self.absolute_path,
            "source": self.source,
            "original_path": self.original_path,
        }


@dataclass(frozen=True)
class ArtifactLimits:
    """单次制品收集的硬预算。

    ``scan_max_depth=0`` 只扫描制品根目录中的文件而不向下递归；各项上限均可设为零，
    以禁用对应资源。
    """

    scan_max_entries: int = 512
    scan_max_depth: int = 8
    max_artifacts: int = 16
    max_single_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_pixels: int = 40_000_000
    max_frames: int = 120

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass
class ArtifactCollectionResult:
    """已归档图片和有界丢弃诊断；调用方必须显式读取 ``artifacts``。"""

    artifacts: list[CodexImageArtifact] = field(default_factory=list)
    dropped_count: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    scan_truncated: bool = False


@dataclass(frozen=True)
class _ScannedEntry:
    name: str
    path: Path
    is_symlink: bool
    is_directory: bool
    is_file: bool
    identity: tuple[int, int, int, int, int] | None = None
    metadata_error: bool = False


@dataclass
class _ArtifactScanResult:
    paths: list[Path] = field(default_factory=list)
    dropped_reasons: Counter[str] = field(default_factory=Counter)
    scan_reasons: Counter[str] = field(default_factory=Counter)
    truncated: bool = False


class _ArtifactRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean_path_text(value: str) -> str:
    if len(value) > MAX_REFERENCED_PATH_CHARS or any(ord(char) < 32 for char in value):
        return ""
    text = value.strip().strip("`").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    text = text.strip("\"'").strip()
    return text.rstrip(".,;，；。")


def _path_from_file_uri(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "file":
        return None
    path_text = parsed.path
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path_text = f"//{parsed.netloc}{path_text}"
    return Path(url2pathname(unquote(path_text)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _path_key(path: Path) -> str:
    """仅在宿主文件系统本身大小写不敏感时折叠路径。"""

    return os.path.normcase(str(path.resolve(strict=False)))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return stat_identity(left) == stat_identity(right)


def _candidate_paths(raw_value: str, *, cwd: Path, artifact_dir: Path) -> list[Path]:
    value = _clean_path_text(raw_value)
    if not value:
        return []
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return []
    file_uri_path = _path_from_file_uri(value)
    if file_uri_path is not None:
        return [file_uri_path]

    path = Path(value)
    if path.is_absolute():
        return [path]
    return [cwd / path, artifact_dir / path]


def _resolve_image_path(
    raw_value: str,
    *,
    cwd: Path,
    artifact_dir: Path,
) -> Path | None:
    allowed_roots = [cwd, artifact_dir]
    for candidate in _candidate_paths(raw_value, cwd=cwd, artifact_dir=artifact_dir):
        if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            before = os.lstat(candidate)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                continue
            resolved = candidate.resolve(strict=True)
            after = os.lstat(candidate)
        except OSError:
            continue
        if not _same_identity(before, after):
            continue
        if any(_is_within(resolved, root) for root in allowed_roots):
            return resolved
    return None


def _referenced_image_values(text: str) -> Iterator[str]:
    yield from (match.group(1) for match in MARKDOWN_IMAGE_RE.finditer(text or ""))
    yield from (match.group(1) for match in IMAGE_LINE_RE.finditer(text or ""))


def _scan_directory_entries(
    directory: Path,
    *,
    remaining_entries: int,
) -> tuple[list[_ScannedEntry], int, bool]:
    entries: list[_ScannedEntry] = []
    consumed = 0
    truncated = False
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if consumed >= remaining_entries:
                truncated = True
                break
            consumed += 1
            try:
                entry_info = entry.stat(follow_symlinks=False)
                is_symlink = entry.is_symlink() or stat.S_ISLNK(entry_info.st_mode)
                is_directory = stat.S_ISDIR(entry_info.st_mode)
                is_file = stat.S_ISREG(entry_info.st_mode)
                identity = stat_identity(entry_info)
                metadata_error = False
            except OSError:
                is_symlink = False
                is_directory = False
                is_file = False
                identity = None
                metadata_error = True
            entries.append(
                _ScannedEntry(
                    name=entry.name,
                    path=Path(entry.path),
                    is_symlink=is_symlink,
                    is_directory=is_directory,
                    is_file=is_file,
                    identity=identity,
                    metadata_error=metadata_error,
                )
            )
    entries.sort(key=lambda item: item.name.casefold())
    return entries, consumed, truncated


def _scan_root_identity(
    artifact_dir: Path,
    result: _ArtifactScanResult,
) -> tuple[int, int, int, int, int] | None:
    try:
        root_info = os.lstat(artifact_dir)
    except FileNotFoundError:
        return None
    except OSError:
        result.truncated = True
        result.scan_reasons["scan_error"] += 1
        return None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        result.truncated = True
        result.scan_reasons["unsafe_scan_root"] += 1
        return None
    return stat_identity(root_info)


def _directory_identity_is_current(
    directory: Path,
    expected_identity: tuple[int, int, int, int, int],
) -> bool:
    try:
        current = os.lstat(directory)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and stat.S_ISDIR(current.st_mode)
        and stat_identity(current) == expected_identity
    )


def _record_scanned_entry(
    entry: _ScannedEntry,
    *,
    depth: int,
    limits: ArtifactLimits,
    directories: deque[tuple[Path, int, tuple[int, int, int, int, int]]],
    result: _ArtifactScanResult,
) -> None:
    suffix_is_image = entry.path.suffix.lower() in IMAGE_EXTENSIONS
    if entry.metadata_error:
        if suffix_is_image:
            result.dropped_reasons["metadata_error"] += 1
        return
    if entry.is_symlink:
        if suffix_is_image:
            result.dropped_reasons["symlink"] += 1
        return
    if entry.is_directory:
        if depth >= limits.scan_max_depth:
            result.truncated = True
            result.scan_reasons["scan_max_depth"] += 1
        elif entry.identity is not None:
            directories.append((entry.path, depth + 1, entry.identity))
        return
    if not suffix_is_image:
        return
    if entry.is_file:
        result.paths.append(entry.path)
    else:
        result.dropped_reasons["not_regular_file"] += 1


def _artifact_images(artifact_dir: Path, limits: ArtifactLimits) -> _ArtifactScanResult:
    """按条目数和深度预算扫描本任务目录，不跟随任何链接。"""

    result = _ArtifactScanResult()
    root_identity = _scan_root_identity(artifact_dir, result)
    if root_identity is None:
        return result

    directories: deque[tuple[Path, int, tuple[int, int, int, int, int]]] = deque(
        [(artifact_dir, 0, root_identity)]
    )
    scanned_entries = 0
    while directories:
        directory, depth, expected_identity = directories.popleft()
        if not _directory_identity_is_current(directory, expected_identity):
            result.truncated = True
            result.scan_reasons["scan_source_changed"] += 1
            continue
        try:
            entries, consumed, entry_truncated = _scan_directory_entries(
                directory,
                remaining_entries=max(0, limits.scan_max_entries - scanned_entries),
            )
        except OSError:
            result.truncated = True
            result.scan_reasons["scan_error"] += 1
            continue
        scanned_entries += consumed
        if entry_truncated:
            result.truncated = True
            result.scan_reasons["scan_max_entries"] += 1

        for entry in entries:
            _record_scanned_entry(
                entry,
                depth=depth,
                limits=limits,
                directories=directories,
                result=result,
            )

        if entry_truncated:
            break

    result.paths.sort(key=lambda item: str(item).casefold())
    return result


def _safe_open_source(source_path: Path) -> tuple[int, os.stat_result]:
    try:
        before = os.lstat(source_path)
    except OSError as exc:
        raise _ArtifactRejected("source_unavailable") from exc
    if stat.S_ISLNK(before.st_mode):
        raise _ArtifactRejected("symlink")
    if not stat.S_ISREG(before.st_mode):
        raise _ArtifactRejected("not_regular_file")
    if before.st_nlink != 1:
        raise _ArtifactRejected("hardlink")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source_path, flags)
    except OSError as exc:
        raise _ArtifactRejected("source_open_error") from exc
    try:
        opened = os.fstat(source_fd)
        current = os.lstat(source_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_ISLNK(current.st_mode)
            or not _same_identity(before, opened)
            or not _same_identity(opened, current)
        ):
            raise _ArtifactRejected("source_changed")
    except Exception:
        os.close(source_fd)
        raise
    return source_fd, opened


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while copying artifact")
        view = view[written:]


def _verify_source_unchanged(
    source_fd: int,
    source_path: Path,
    initial: os.stat_result,
) -> None:
    try:
        after_fd = os.fstat(source_fd)
        after_path = os.lstat(source_path)
    except OSError as exc:
        raise _ArtifactRejected("source_changed") from exc
    if (
        after_fd.st_nlink != 1
        or stat.S_ISLNK(after_path.st_mode)
        or not _same_identity(initial, after_fd)
        or not _same_identity(after_fd, after_path)
    ):
        raise _ArtifactRejected("source_changed")


def _check_copy_budget(size: int, *, committed_bytes: int, limits: ArtifactLimits) -> None:
    if size > limits.max_single_bytes:
        raise _ArtifactRejected("single_bytes_limit")
    if committed_bytes + size > limits.max_total_bytes:
        raise _ArtifactRejected("total_bytes_limit")


def _copy_source_bytes(
    source_fd: int,
    temp_fd: int,
    *,
    committed_bytes: int,
    limits: ArtifactLimits,
) -> int:
    copied_bytes = 0
    while True:
        chunk = os.read(source_fd, COPY_CHUNK_BYTES)
        if not chunk:
            return copied_bytes
        copied_bytes += len(chunk)
        _check_copy_budget(
            copied_bytes,
            committed_bytes=committed_bytes,
            limits=limits,
        )
        _write_all(temp_fd, chunk)


def _close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _copy_validated_image(
    source_path: Path,
    destination: Path,
    *,
    limits: ArtifactLimits,
    committed_bytes: int,
) -> tuple[int | None, str | None]:
    source_fd: int | None = None
    temp_fd: int | None = None
    temp_path: Path | None = None
    try:
        source_fd, initial = _safe_open_source(source_path)
        _check_copy_budget(
            initial.st_size,
            committed_bytes=committed_bytes,
            limits=limits,
        )

        temp_fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temp_path = Path(raw_temp_path)
        copied_bytes = _copy_source_bytes(
            source_fd,
            temp_fd,
            committed_bytes=committed_bytes,
            limits=limits,
        )

        _verify_source_unchanged(source_fd, source_path, initial)
        if copied_bytes != initial.st_size:
            raise _ArtifactRejected("source_changed")
        if os.fstat(temp_fd).st_size != copied_bytes:
            raise _ArtifactRejected("copy_size_mismatch")

        if limits.max_single_bytes <= 0:
            raise _ArtifactRejected("single_bytes_limit")
        if limits.max_pixels <= 0:
            raise _ArtifactRejected("pixel_limit")
        if limits.max_frames <= 0:
            raise _ArtifactRejected("frame_limit")
        try:
            validate_image_fd(
                temp_fd,
                expected_suffix=source_path.suffix.lower(),
                limits=ImageValidationLimits(
                    max_bytes=limits.max_single_bytes,
                    max_pixels=limits.max_pixels,
                    max_frames=limits.max_frames,
                ),
            )
        except ImageValidationError as exc:
            reason = (
                "invalid_image"
                if exc.reason in {"empty_image", "invalid_container", "invalid_type"}
                else exc.reason
            )
            raise _ArtifactRejected(reason) from exc
        os.fsync(temp_fd)

        os.close(temp_fd)
        temp_fd = None
        os.close(source_fd)
        source_fd = None
        os.replace(temp_path, destination)
        temp_path = None
        return copied_bytes, None
    except _ArtifactRejected as exc:
        return None, exc.reason
    except OSError:
        return None, "copy_error"
    finally:
        _close_fd(temp_fd)
        _close_fd(source_fd)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _prepare_archive_directory(session_dir: Path, images_dir: Path) -> tuple[Path, Path]:
    """确认图片归档目录是会话目录内的真实目录，而不是链接跳板。"""

    try:
        session_root = session_dir.resolve(strict=True)
        images_dir.mkdir(parents=True, exist_ok=True)
        archive_info = os.lstat(images_dir)
        if stat.S_ISLNK(archive_info.st_mode) or not stat.S_ISDIR(archive_info.st_mode):
            raise ValueError("Codex image archive must be a regular directory")
        archive_dir = images_dir.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Codex image archive could not be prepared") from exc
    if not archive_dir.is_relative_to(session_root):
        raise ValueError("Codex image archive must stay inside the session directory")
    return session_root, archive_dir


def _discover_candidates(
    final_text: str,
    referenced_paths: list[str] | None,
    *,
    cwd: Path,
    artifact_dir: Path,
    limits: ArtifactLimits,
) -> tuple[list[tuple[Path, str, str]], Counter[str], int, bool]:
    candidates: list[tuple[Path, str, str]] = []
    seen: set[str] = set()
    reasons: Counter[str] = Counter()
    dropped_count = 0

    reference_values = chain(referenced_paths or (), _referenced_image_values(final_text))
    for raw_value in reference_values:
        if not isinstance(raw_value, str):
            dropped_count += 1
            reasons["invalid_reference"] += 1
            continue
        resolved = _resolve_image_path(raw_value, cwd=cwd, artifact_dir=artifact_dir)
        if resolved is None:
            continue
        key = _path_key(resolved)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((resolved, "reference", _clean_path_text(raw_value)))

    scan_result = _artifact_images(artifact_dir, limits)
    reasons.update(scan_result.dropped_reasons)
    reasons.update(scan_result.scan_reasons)
    dropped_count += sum(scan_result.dropped_reasons.values())
    for path in scan_result.paths:
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((path, "artifact", str(path)))
    return candidates, reasons, dropped_count, scan_result.truncated


def collect_image_artifacts(
    final_text: str,
    *,
    referenced_paths: list[str] | None = None,
    cwd: Path,
    artifact_dir: Path,
    session_dir: Path,
    images_dir: Path,
    job_id: int,
    limits: ArtifactLimits | None = None,
) -> ArtifactCollectionResult:
    """收集显式引用或本任务目录中的图片，并复制到会话归档。"""

    if type(job_id) is not int or not 1 <= job_id <= 2**63 - 1:
        raise ValueError("job_id must be a positive 64-bit integer")
    effective_limits = limits or ArtifactLimits()
    session_root, archive_dir = _prepare_archive_directory(session_dir, images_dir)
    candidates, reason_counts, dropped_count, scan_truncated = _discover_candidates(
        final_text,
        referenced_paths,
        cwd=cwd,
        artifact_dir=artifact_dir,
        limits=effective_limits,
    )

    artifacts: list[CodexImageArtifact] = []
    committed_bytes = 0
    for source_path, source, original_path in candidates:
        if len(artifacts) >= effective_limits.max_artifacts:
            dropped_count += 1
            reason_counts["artifact_count_limit"] += 1
            continue

        allowed_roots = [artifact_dir] if source == "artifact" else [cwd, artifact_dir]
        if not any(_is_within(source_path, root) for root in allowed_roots):
            dropped_count += 1
            reason_counts["unsafe_source_path"] += 1
            continue

        artifact_index = len(artifacts) + 1
        suffix = source_path.suffix.lower()
        destination = archive_dir / f"job-{job_id:04d}-{artifact_index:02d}{suffix}"
        copied_bytes, rejection_reason = _copy_validated_image(
            source_path,
            destination,
            limits=effective_limits,
            committed_bytes=committed_bytes,
        )
        if rejection_reason is not None or copied_bytes is None:
            dropped_count += 1
            reason_counts[rejection_reason or "copy_error"] += 1
            continue

        committed_bytes += copied_bytes
        relative_path = destination.relative_to(session_root).as_posix()
        artifacts.append(
            CodexImageArtifact(
                path=relative_path,
                absolute_path=str(destination.resolve()),
                source=source,
                original_path=original_path,
            )
        )

    return ArtifactCollectionResult(
        artifacts=artifacts,
        dropped_count=dropped_count,
        reasons=dict(sorted(reason_counts.items())),
        scan_truncated=scan_truncated,
    )
