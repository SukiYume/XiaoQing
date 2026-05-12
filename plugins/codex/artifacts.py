from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\n]+)\)")
IMAGE_LINE_RE = re.compile(r"(?im)^\s*(?:图片|image|img|artifact)\s*[:：]\s*(.+?)\s*$")


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
    except ValueError:
        return False
    return True


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
    allowed_roots = [cwd, artifact_dir, session_dir]
    for candidate in _candidate_paths(raw_value, cwd=cwd, artifact_dir=artifact_dir):
        if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if not candidate.exists() or not candidate.is_file():
            continue
        if any(_is_within(candidate, root) for root in allowed_roots):
            return candidate.resolve()
    return None


def _referenced_image_values(text: str) -> list[str]:
    values: list[str] = []
    values.extend(match.group(1) for match in MARKDOWN_IMAGE_RE.finditer(text or ""))
    values.extend(match.group(1) for match in IMAGE_LINE_RE.finditer(text or ""))
    return values


def _artifact_images(artifact_dir: Path) -> list[Path]:
    if not artifact_dir.exists():
        return []
    return sorted(
        (
            item.resolve()
            for item in artifact_dir.rglob("*")
            if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS
        ),
        key=lambda item: str(item).lower(),
    )


def _generated_images(
    generated_images_dir: Path | None,
    *,
    started_at: float | None,
    finished_at: float | None,
) -> list[Path]:
    if generated_images_dir is None or not generated_images_dir.exists():
        return []
    start = (started_at or 0) - 10
    end = (finished_at or time.time()) + 10
    candidates: list[Path] = []
    for item in generated_images_dir.rglob("*"):
        if not item.is_file() or item.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            modified_at = item.stat().st_mtime
        except OSError:
            continue
        if start <= modified_at <= end:
            candidates.append(item.resolve())
    return sorted(candidates, key=lambda item: (item.stat().st_mtime, str(item).lower()))


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
) -> list[CodexImageArtifact]:
    images_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[tuple[Path, str, str]] = []
    seen: set[str] = set()

    reference_values = [*(referenced_paths or []), *_referenced_image_values(final_text)]
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

    for resolved, source in [
        *((path, "artifact") for path in _artifact_images(artifact_dir)),
        *((path, "generated_images") for path in _generated_images(
            generated_images_dir,
            started_at=started_at,
            finished_at=finished_at,
        )),
    ]:
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append((resolved, source, str(resolved)))

    artifacts: list[CodexImageArtifact] = []
    for index, (source_path, source, original_path) in enumerate(candidates, start=1):
        suffix = source_path.suffix.lower()
        dest = images_dir / f"job-{job_id:04d}-{index:02d}{suffix}"
        if source_path.resolve() != dest.resolve():
            shutil.copy2(source_path, dest)
        relative_path = dest.relative_to(session_dir).as_posix()
        artifacts.append(
            CodexImageArtifact(
                path=relative_path,
                absolute_path=str(dest.resolve()),
                source=source,
                original_path=original_path,
            )
        )
    return artifacts
