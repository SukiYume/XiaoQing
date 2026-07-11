from __future__ import annotations

import os
import re
import stat
import tempfile
import warnings
from collections import Counter, deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import overload
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
IMAGE_FORMAT_EXTENSIONS = {
    "GIF": {".gif"},
    "JPEG": {".jpeg", ".jpg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
}
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\n]+)\)")
IMAGE_LINE_RE = re.compile(r"(?im)^\s*(?:图片|image|img|artifact)\s*[:：]\s*(.+?)\s*$")
COPY_CHUNK_BYTES = 1024 * 1024


def default_generated_images_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "generated_images"


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
    """Hard limits for one artifact collection pass.

    ``scan_max_depth=0`` scans files in the artifact root but does not descend.
    All limits may be set to zero to disable the corresponding resource.
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
class ArtifactCollectionResult(Sequence[CodexImageArtifact]):
    """Collected artifacts plus bounded-loss diagnostics.

    ``dropped_count`` counts known image candidates that were rejected. Scan
    truncation can hide an unknown number of candidates, so it is reported
    separately by ``scan_truncated``. ``reasons`` also records scan-limit events.
    The sequence methods preserve the previous list-like call-site behaviour.
    """

    artifacts: list[CodexImageArtifact] = field(default_factory=list)
    dropped_count: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    scan_truncated: bool = False

    def __len__(self) -> int:
        return len(self.artifacts)

    def __iter__(self) -> Iterator[CodexImageArtifact]:
        return iter(self.artifacts)

    @overload
    def __getitem__(self, index: int) -> CodexImageArtifact: ...

    @overload
    def __getitem__(self, index: slice) -> list[CodexImageArtifact]: ...

    def __getitem__(self, index: int | slice) -> CodexImageArtifact | list[CodexImageArtifact]:
        return self.artifacts[index]


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


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _stat_identity(left) == _stat_identity(right)


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
    session_dir: Path,
) -> Path | None:
    del session_dir
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
                identity = _stat_identity(entry_info)
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


def _artifact_images(artifact_dir: Path, limits: ArtifactLimits) -> _ArtifactScanResult:
    result = _ArtifactScanResult()
    try:
        root_info = os.lstat(artifact_dir)
    except FileNotFoundError:
        return result
    except OSError:
        result.truncated = True
        result.scan_reasons["scan_error"] += 1
        return result
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        result.truncated = True
        result.scan_reasons["unsafe_scan_root"] += 1
        return result

    directories: deque[tuple[Path, int, tuple[int, int, int, int, int]]] = deque(
        [(artifact_dir, 0, _stat_identity(root_info))]
    )
    scanned_entries = 0
    while directories:
        directory, depth, expected_identity = directories.popleft()
        try:
            current_directory = os.lstat(directory)
        except OSError:
            result.truncated = True
            result.scan_reasons["scan_source_changed"] += 1
            continue
        if (
            stat.S_ISLNK(current_directory.st_mode)
            or not stat.S_ISDIR(current_directory.st_mode)
            or _stat_identity(current_directory) != expected_identity
        ):
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
            suffix_is_image = entry.path.suffix.lower() in IMAGE_EXTENSIONS
            if entry.metadata_error:
                if suffix_is_image:
                    result.dropped_reasons["metadata_error"] += 1
                continue
            if entry.is_symlink:
                if suffix_is_image:
                    result.dropped_reasons["symlink"] += 1
                continue
            if entry.is_directory:
                if depth >= limits.scan_max_depth:
                    result.truncated = True
                    result.scan_reasons["scan_max_depth"] += 1
                elif entry.identity is not None:
                    directories.append((entry.path, depth + 1, entry.identity))
                continue
            if suffix_is_image:
                if entry.is_file:
                    result.paths.append(entry.path)
                else:
                    result.dropped_reasons["not_regular_file"] += 1

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


def _duplicate_fd_stream(fd: int):
    os.lseek(fd, 0, os.SEEK_SET)
    return os.fdopen(os.dup(fd), "rb")


def _validate_image_fd(fd: int, *, suffix: str, limits: ArtifactLimits) -> str | None:
    try:
        from PIL import Image
    except ImportError:
        return "pillow_unavailable"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with _duplicate_fd_stream(fd) as stream, Image.open(stream) as image:
                image_format = str(image.format or "").upper()
                if suffix not in IMAGE_FORMAT_EXTENSIONS.get(image_format, set()):
                    return "format_mismatch"
                width, height = image.size
                if width <= 0 or height <= 0:
                    return "invalid_dimensions"
                if width * height > limits.max_pixels:
                    return "pixel_limit"
                image.verify()

            with _duplicate_fd_stream(fd) as stream, Image.open(stream) as image:
                if str(image.format or "").upper() != image_format:
                    return "format_changed"
                frame_count = 0
                while True:
                    if frame_count >= limits.max_frames:
                        return "frame_limit"
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        return "invalid_dimensions"
                    if width * height > limits.max_pixels:
                        return "pixel_limit"
                    image.load()
                    frame_count += 1
                    try:
                        image.seek(frame_count)
                    except EOFError:
                        break
    except Exception:
        return "invalid_image"
    return None


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
        if initial.st_size > limits.max_single_bytes:
            raise _ArtifactRejected("single_bytes_limit")
        if committed_bytes + initial.st_size > limits.max_total_bytes:
            raise _ArtifactRejected("total_bytes_limit")

        temp_fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temp_path = Path(raw_temp_path)
        copied_bytes = 0
        while True:
            chunk = os.read(source_fd, COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied_bytes += len(chunk)
            if copied_bytes > limits.max_single_bytes:
                raise _ArtifactRejected("single_bytes_limit")
            if committed_bytes + copied_bytes > limits.max_total_bytes:
                raise _ArtifactRejected("total_bytes_limit")
            _write_all(temp_fd, chunk)

        _verify_source_unchanged(source_fd, source_path, initial)
        if copied_bytes != initial.st_size:
            raise _ArtifactRejected("source_changed")
        if os.fstat(temp_fd).st_size != copied_bytes:
            raise _ArtifactRejected("copy_size_mismatch")

        validation_reason = _validate_image_fd(
            temp_fd,
            suffix=source_path.suffix.lower(),
            limits=limits,
        )
        if validation_reason is not None:
            raise _ArtifactRejected(validation_reason)
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
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if source_fd is not None:
            try:
                os.close(source_fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def collect_image_artifacts(
    final_text: str,
    *,
    referenced_paths: list[str] | None = None,
    cwd: Path,
    artifact_dir: Path,
    session_dir: Path,
    images_dir: Path,
    job_id: int,
    generated_images_dir: Path | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
    limits: ArtifactLimits | None = None,
) -> ArtifactCollectionResult:
    # Legacy timing/global-directory arguments are intentionally ignored.
    # Artifacts must be explicitly referenced or written into this job's dir.
    del generated_images_dir, started_at, finished_at
    effective_limits = limits or ArtifactLimits()
    images_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[tuple[Path, str, str]] = []
    seen: set[str] = set()
    reason_counts: Counter[str] = Counter()
    dropped_count = 0

    reference_values = chain(referenced_paths or (), _referenced_image_values(final_text))
    for raw_value in reference_values:
        resolved = _resolve_image_path(
            raw_value,
            cwd=cwd,
            artifact_dir=artifact_dir,
            session_dir=session_dir,
        )
        if resolved is None:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append((resolved, "reference", _clean_path_text(raw_value)))

    scan_result = _artifact_images(artifact_dir, effective_limits)
    reason_counts.update(scan_result.dropped_reasons)
    reason_counts.update(scan_result.scan_reasons)
    dropped_count += sum(scan_result.dropped_reasons.values())
    for path in scan_result.paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append((path, "artifact", str(path)))

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
        destination = images_dir / f"job-{job_id:04d}-{artifact_index:02d}{suffix}"
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
        relative_path = destination.relative_to(session_dir).as_posix()
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
        scan_truncated=scan_result.truncated,
    )
