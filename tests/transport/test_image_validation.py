# 验证不可信图片在尺寸、格式和解码边界受到统一约束。
"""Shared untrusted-image validation boundaries."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from PIL import Image

from core.image_validation import (
    ImageValidationError,
    ImageValidationLimits,
    validate_image_bytes,
    validate_image_fd,
    validate_image_path,
)


def _image_bytes(image_format: str = "PNG", *, frames: int = 1) -> bytes:
    buffer = io.BytesIO()
    images = [Image.new("RGB", (4, 3), color) for color in ("red", "blue", "green")[:frames]]
    images[0].save(
        buffer,
        format        = image_format,
        save_all      = frames > 1,
        append_images = images[1:],
        duration      = 10,
        loop          = 0,
    )
    return buffer.getvalue()


def _limits(**overrides: int) -> ImageValidationLimits:
    return ImageValidationLimits(
        max_bytes  = overrides.get("max_bytes", 1024 * 1024),
        max_pixels = overrides.get("max_pixels", 10_000),
        max_frames = overrides.get("max_frames", 10),
    )


def test_valid_image_is_fully_decoded_and_uses_content_extension() -> None:
    validated = validate_image_bytes(_image_bytes(), limits=_limits())

    assert (validated.format, validated.extension) == ("PNG", ".png")
    assert (validated.width, validated.height, validated.frames, validated.mode) == (
        4,
        3,
        1,
        "RGB",
    )


@pytest.mark.parametrize("mutation", ("truncated", "trailing"))
def test_container_boundary_rejects_truncation_and_polyglot_bytes(mutation: str) -> None:
    payload = _image_bytes()
    payload = payload[:-12] if mutation == "truncated" else payload + b"POLYGLOT"

    with pytest.raises(ImageValidationError) as exc_info:
        validate_image_bytes(payload, limits=_limits())

    assert exc_info.value.reason in {"invalid_container", "invalid_image"}


def test_every_animation_frame_is_decoded_under_the_frame_budget() -> None:
    payload = _image_bytes("GIF", frames=3)

    with pytest.raises(ImageValidationError) as exc_info:
        validate_image_bytes(payload, limits=_limits(max_frames=2))

    assert exc_info.value.reason == "frame_limit"


def test_declared_format_and_local_suffix_must_match_real_content(tmp_path: Path) -> None:
    payload = _image_bytes()
    with pytest.raises(ImageValidationError) as declared:
        validate_image_bytes(payload, limits=_limits(), expected_format="JPEG")
    assert declared.value.reason == "format_mismatch"

    path = tmp_path / "image.jpg"
    path.write_bytes(payload)
    with pytest.raises(ImageValidationError) as suffix:
        validate_image_path(path, limits=_limits())
    assert suffix.value.reason == "format_mismatch"


def test_fd_validation_restores_caller_offset(tmp_path: Path) -> None:
    path = tmp_path / "image.png"
    path.write_bytes(_image_bytes())
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        os.lseek(fd, 3, os.SEEK_SET)
        assert validate_image_fd(fd, limits=_limits(), expected_suffix=".png").format == "PNG"
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
    finally:
        os.close(fd)


def test_local_validation_rejects_symlinks_and_hardlinks(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(_image_bytes())

    symlink = tmp_path / "symlink.png"
    try:
        symlink.symlink_to(source)
    except OSError:
        symlink = None
    if symlink is not None:
        with pytest.raises(ImageValidationError) as symlink_error:
            validate_image_path(symlink, limits=_limits())
        assert symlink_error.value.reason == "symlink"

    hardlink = tmp_path / "hardlink.png"
    try:
        os.link(source, hardlink)
    except OSError:
        return
    with pytest.raises(ImageValidationError) as hardlink_error:
        validate_image_path(hardlink, limits=_limits())
    assert hardlink_error.value.reason == "hardlink"


def test_resource_budgets_fail_before_acceptance() -> None:
    payload = _image_bytes()
    with pytest.raises(ImageValidationError) as bytes_error:
        validate_image_bytes(payload, limits=_limits(max_bytes=len(payload) - 1))
    assert bytes_error.value.reason == "bytes_limit"

    with pytest.raises(ImageValidationError) as pixels_error:
        validate_image_bytes(payload, limits=_limits(max_pixels=11))
    assert pixels_error.value.reason == "pixel_limit"
