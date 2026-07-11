from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from plugins.codex import artifacts as artifact_module
from plugins.codex.artifacts import (
    ArtifactCollectionResult,
    ArtifactLimits,
    collect_image_artifacts,
)


def _save_png(path: Path, *, size: tuple[int, int] = (8, 8), color: str = "red") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG", compress_level=0)


def _save_gif(path: Path, *, frames: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    images = [Image.new("RGB", (8, 8), (index * 50, 0, 0)) for index in range(frames)]
    images[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=10,
        loop=0,
    )


def _paths(base: Path) -> tuple[Path, Path, Path, Path]:
    cwd = base / "cwd"
    session_dir = base / "session"
    artifact_dir = session_dir / "jobs" / "job-0001" / "artifacts"
    images_dir = session_dir / "images"
    cwd.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return cwd, session_dir, artifact_dir, images_dir


def _collect(
    base: Path,
    *,
    limits: ArtifactLimits | None = None,
    final_text: str = "",
    referenced_paths: list[str] | None = None,
) -> ArtifactCollectionResult:
    cwd, session_dir, artifact_dir, images_dir = _paths(base)
    return collect_image_artifacts(
        final_text,
        referenced_paths=referenced_paths,
        cwd=cwd,
        artifact_dir=artifact_dir,
        session_dir=session_dir,
        images_dir=images_dir,
        job_id=1,
        limits=limits,
    )


def test_collects_real_image_and_preserves_sequence_compatibility(tmp_path: Path) -> None:
    _, session_dir, artifact_dir, _ = _paths(tmp_path)
    source = artifact_dir / "chart.png"
    _save_png(source)

    result = _collect(tmp_path)

    assert isinstance(result, ArtifactCollectionResult)
    assert len(result) == 1
    assert list(result) == result.artifacts
    assert result[0] == result.artifacts[0]
    assert result[:] == result.artifacts
    assert result.dropped_count == 0
    assert result.reasons == {}
    assert result.scan_truncated is False
    assert result[0].path == "images/job-0001-01.png"
    destination = Path(result[0].absolute_path)
    assert destination.is_relative_to(session_dir)
    with Image.open(destination) as copied:
        assert copied.format == "PNG"
        copied.verify()


@pytest.mark.parametrize("kind", ["fake", "truncated", "wrong_suffix"])
def test_rejects_fake_truncated_and_suffix_mismatched_images(tmp_path: Path, kind: str) -> None:
    _, _, artifact_dir, images_dir = _paths(tmp_path)
    source = artifact_dir / "bad.png"
    if kind == "fake":
        source.write_bytes(b"not an image")
    elif kind == "truncated":
        _save_png(source)
        source.write_bytes(source.read_bytes()[:-12])
    else:
        Image.new("RGB", (8, 8), "blue").save(source, format="JPEG")

    result = _collect(tmp_path)

    assert len(result) == 0
    assert result.dropped_count == 1
    expected_reason = "format_mismatch" if kind == "wrong_suffix" else "invalid_image"
    assert result.reasons[expected_reason] == 1
    assert not list(images_dir.glob("job-*"))
    assert not list(images_dir.glob(".*.tmp"))


def test_enforces_pixel_and_frame_limits(tmp_path: Path) -> None:
    pixel_base = tmp_path / "pixels"
    _, _, pixel_artifacts, _ = _paths(pixel_base)
    _save_png(pixel_artifacts / "large.png", size=(20, 20))

    pixel_result = _collect(pixel_base, limits=ArtifactLimits(max_pixels=399))

    assert len(pixel_result) == 0
    assert pixel_result.reasons["pixel_limit"] == 1

    frame_base = tmp_path / "frames"
    _, _, frame_artifacts, _ = _paths(frame_base)
    _save_gif(frame_artifacts / "animated.gif", frames=3)

    frame_result = _collect(frame_base, limits=ArtifactLimits(max_frames=2))

    assert len(frame_result) == 0
    assert frame_result.reasons["frame_limit"] == 1


def test_enforces_single_and_total_byte_limits(tmp_path: Path) -> None:
    single_base = tmp_path / "single"
    _, _, single_artifacts, _ = _paths(single_base)
    single_source = single_artifacts / "one.png"
    _save_png(single_source, size=(16, 16))

    single_result = _collect(
        single_base,
        limits=ArtifactLimits(max_single_bytes=single_source.stat().st_size - 1),
    )

    assert len(single_result) == 0
    assert single_result.reasons["single_bytes_limit"] == 1

    total_base = tmp_path / "total"
    _, _, total_artifacts, _ = _paths(total_base)
    first = total_artifacts / "a.png"
    second = total_artifacts / "b.png"
    _save_png(first, size=(16, 16), color="red")
    _save_png(second, size=(16, 16), color="blue")

    total_result = _collect(
        total_base,
        limits=ArtifactLimits(max_total_bytes=first.stat().st_size),
    )

    assert len(total_result) == 1
    assert total_result.dropped_count == 1
    assert total_result.reasons["total_bytes_limit"] == 1


def test_enforces_artifact_count_limit(tmp_path: Path) -> None:
    _, _, artifact_dir, _ = _paths(tmp_path)
    for index in range(4):
        _save_png(artifact_dir / f"{index}.png", color=("red" if index % 2 else "blue"))

    result = _collect(tmp_path, limits=ArtifactLimits(max_artifacts=2))

    assert len(result) == 2
    assert result.dropped_count == 2
    assert result.reasons["artifact_count_limit"] == 2


def test_scan_entry_and_depth_limits_are_bounded_and_reported(tmp_path: Path) -> None:
    entry_base = tmp_path / "entries"
    _, _, entry_artifacts, _ = _paths(entry_base)
    _save_png(entry_artifacts / "a.png")
    _save_png(entry_artifacts / "b.png")

    entry_result = _collect(entry_base, limits=ArtifactLimits(scan_max_entries=1))

    assert len(entry_result) <= 1
    assert entry_result.scan_truncated is True
    assert entry_result.reasons["scan_max_entries"] == 1

    depth_base = tmp_path / "depth"
    _, _, depth_artifacts, _ = _paths(depth_base)
    _save_png(depth_artifacts / "nested" / "inside.png")

    depth_result = _collect(depth_base, limits=ArtifactLimits(scan_max_depth=0))

    assert len(depth_result) == 0
    assert depth_result.scan_truncated is True
    assert depth_result.reasons["scan_max_depth"] == 1


def test_rejects_symlinks_and_hardlinks(tmp_path: Path) -> None:
    symlink_base = tmp_path / "symlink"
    symlink_cwd, _, symlink_artifacts, _ = _paths(symlink_base)
    symlink_target = symlink_cwd / "target.png"
    _save_png(symlink_target)
    try:
        (symlink_artifacts / "linked.png").symlink_to(symlink_target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    symlink_result = _collect(symlink_base)

    assert len(symlink_result) == 0
    assert symlink_result.dropped_count == 1
    assert symlink_result.reasons["symlink"] == 1

    hardlink_base = tmp_path / "hardlink"
    hardlink_cwd, _, hardlink_artifacts, _ = _paths(hardlink_base)
    hardlink_target = hardlink_cwd / "target.png"
    _save_png(hardlink_target)
    try:
        os.link(hardlink_target, hardlink_artifacts / "linked.png")
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")

    hardlink_result = _collect(hardlink_base)

    assert len(hardlink_result) == 0
    assert hardlink_result.dropped_count == 1
    assert hardlink_result.reasons["hardlink"] == 1


def test_copy_failure_and_source_identity_failure_clean_random_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_base = tmp_path / "replace"
    _, _, replace_artifacts, replace_images = _paths(replace_base)
    _save_png(replace_artifacts / "image.png")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(artifact_module.os, "replace", fail_replace)
    replace_result = _collect(replace_base)

    assert len(replace_result) == 0
    assert replace_result.reasons["copy_error"] == 1
    assert not list(replace_images.glob(".*.tmp"))

    monkeypatch.undo()
    identity_base = tmp_path / "identity"
    _, _, identity_artifacts, identity_images = _paths(identity_base)
    _save_png(identity_artifacts / "image.png")

    def fail_identity(*_args, **_kwargs) -> None:
        raise artifact_module._ArtifactRejected("source_changed")  # noqa: SLF001

    monkeypatch.setattr(artifact_module, "_verify_source_unchanged", fail_identity)
    identity_result = _collect(identity_base)

    assert len(identity_result) == 0
    assert identity_result.reasons["source_changed"] == 1
    assert not list(identity_images.glob(".*.tmp"))
