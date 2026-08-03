"""Shared validation for untrusted image bytes and local image files."""

from __future__ import annotations

import io
import os
import stat
import struct
import warnings
import zlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn, cast

from PIL import Image, UnidentifiedImageError

DEFAULT_IMAGE_FORMAT_EXTENSIONS: Final[Mapping[str, str]] = {
    "BMP": ".bmp",
    "GIF": ".gif",
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}
DEFAULT_IMAGE_FORMAT_SUFFIXES: Final[Mapping[str, frozenset[str]]] = {
    "BMP": frozenset({".bmp"}),
    "GIF": frozenset({".gif"}),
    "JPEG": frozenset({".jpeg", ".jpg"}),
    "PNG": frozenset({".png"}),
    "WEBP": frozenset({".webp"}),
}

_PNG_TRAILER: Final = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_MAX_PNG_CHUNKS: Final = 4_096
_MODE_BYTES_PER_PIXEL: Final[Mapping[str, int]] = {
    "1": 1,
    "L": 1,
    "P": 1,
    "LA": 2,
    "RGB": 3,
    "YCbCr": 3,
    "RGBA": 4,
    "CMYK": 4,
}


class ImageValidationError(ValueError):
    """An untrusted image failed a named validation boundary."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ImageValidationLimits:
    """Resource budgets applied before and during image decoding."""

    max_bytes: int
    max_pixels: int
    max_frames: int = 120
    max_dimension: int | None = None
    max_decoded_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_pixels", "max_frames"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_dimension", "max_decoded_bytes"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer when provided")


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    """Metadata derived from fully decoded image content, never from its name."""

    format: str
    extension: str
    width: int
    height: int
    frames: int
    mode: str


def _reject(reason: str, message: str) -> NoReturn:
    raise ImageValidationError(reason, message)


def _validate_png_container(payload: bytes) -> None:
    if not payload.startswith(_PNG_SIGNATURE):
        _reject("invalid_container", "PNG signature is invalid")
    offset = len(_PNG_SIGNATURE)
    chunk_count = 0
    seen_header = False
    seen_image_data = False
    while offset < len(payload):
        if chunk_count >= _MAX_PNG_CHUNKS or offset + 12 > len(payload):
            _reject("invalid_container", "PNG chunk boundary is invalid")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(payload):
            _reject("invalid_container", "PNG chunk is truncated")
        chunk_type = payload[offset + 4 : offset + 8]
        if len(chunk_type) != 4 or not all(
            65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type
        ):
            _reject("invalid_container", "PNG chunk type is invalid")
        chunk_data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        actual_crc = zlib.crc32(chunk_data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            _reject("invalid_container", "PNG chunk CRC is invalid")

        chunk_count += 1
        offset = end
        if chunk_type == b"IHDR":
            if seen_header or chunk_count != 1 or len(chunk_data) != 13:
                _reject("invalid_container", "PNG header placement is invalid")
            seen_header = True
        elif chunk_type == b"IDAT":
            if not seen_header:
                _reject("invalid_container", "PNG image data precedes its header")
            seen_image_data = True
        elif chunk_type == b"IEND":
            if chunk_data or not seen_header or not seen_image_data or offset != len(payload):
                _reject("invalid_container", "PNG terminator is invalid")
            return
    _reject("invalid_container", "PNG terminator is missing")


def _validate_container(payload: bytes, image_format: str) -> None:
    """Reject truncated files and bytes appended after a complete known container."""

    if image_format == "JPEG" and not payload.endswith(b"\xff\xd9"):
        _reject("invalid_container", "JPEG has trailing or truncated data")
    if image_format == "PNG":
        if not payload.endswith(_PNG_TRAILER):
            _reject("invalid_container", "PNG has trailing or truncated data")
        _validate_png_container(payload)
    if image_format == "WEBP" and (
        len(payload) < 12
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
        or int.from_bytes(payload[4:8], "little") + 8 != len(payload)
    ):
        _reject("invalid_container", "WebP container length is invalid")
    if image_format == "GIF" and not payload.endswith(b";"):
        _reject("invalid_container", "GIF has trailing or truncated data")


def _validate_frame(
    image: Any,
    *,
    limits: ImageValidationLimits,
    image_format: str,
    allowed_modes: Mapping[str, Collection[str]] | None,
) -> tuple[int, int, str]:
    width, height = image.size
    if width <= 0 or height <= 0:
        _reject("invalid_dimensions", "image dimensions must be positive")
    if limits.max_dimension is not None and (
        width > limits.max_dimension or height > limits.max_dimension
    ):
        _reject("dimension_limit", "image dimension limit exceeded")
    if width * height > limits.max_pixels:
        _reject("pixel_limit", "image pixel limit exceeded")

    mode = str(image.mode or "")
    if allowed_modes is not None and mode not in allowed_modes.get(image_format, ()):
        _reject("mode_not_allowed", "image mode is not allowed")
    if limits.max_decoded_bytes is not None:
        bytes_per_pixel = _MODE_BYTES_PER_PIXEL.get(mode, max(1, len(image.getbands())))
        if width * height * bytes_per_pixel > limits.max_decoded_bytes:
            _reject("decoded_bytes_limit", "image decoded-size limit exceeded")
    return width, height, mode


def _decode_all_frames(
    image: Any,
    *,
    limits: ImageValidationLimits,
    image_format: str,
    allowed_modes: Mapping[str, Collection[str]] | None,
    allow_animation: bool,
) -> tuple[int, int, int, str]:
    frame_count = 0
    first_width = 0
    first_height = 0
    first_mode = ""
    while True:
        if frame_count and not allow_animation:
            _reject("animation_not_allowed", "animated images are not allowed")
        if frame_count >= limits.max_frames:
            _reject("frame_limit", "image frame count limit exceeded")
        width, height, mode = _validate_frame(
            image,
            limits=limits,
            image_format=image_format,
            allowed_modes=allowed_modes,
        )
        image.load()
        _validate_frame(
            image,
            limits=limits,
            image_format=image_format,
            allowed_modes=allowed_modes,
        )
        if frame_count == 0:
            first_width, first_height, first_mode = width, height, mode
        frame_count += 1
        try:
            image.seek(frame_count)
        except EOFError:
            return first_width, first_height, frame_count, first_mode


def validate_image_bytes(
    payload: object,
    *,
    limits: ImageValidationLimits,
    format_extensions: Mapping[str, str] = DEFAULT_IMAGE_FORMAT_EXTENSIONS,
    expected_format: str | None = None,
    allowed_modes: Mapping[str, Collection[str]] | None = None,
    allow_animation: bool = True,
) -> ValidatedImage:
    """Validate container integrity and fully decode every bounded image frame.

    ``Image.verify()`` and a second decode are both intentional: the first checks
    structural integrity and invalidates its Pillow object, while the second proves
    the pixels of every permitted frame can actually be decoded. Known containers
    are also required to end at their real terminator so trailing polyglot bytes do
    not survive validation.
    """

    if type(payload) is not bytes:
        _reject("invalid_type", "image payload must be bytes")
    payload_bytes = cast(bytes, payload)
    if not payload_bytes:
        _reject("empty_image", "image payload is empty")
    if len(payload_bytes) > limits.max_bytes:
        _reject("bytes_limit", "image byte limit exceeded")

    normalized_expected = str(expected_format or "").upper() or None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload_bytes)) as candidate:
                image_format = str(candidate.format or "").upper()
                extension = format_extensions.get(image_format)
                if extension is None:
                    _reject("unsupported_format", "image format is not supported")
                if normalized_expected is not None and image_format != normalized_expected:
                    _reject("format_mismatch", "declared image type does not match its content")
                _validate_frame(
                    candidate,
                    limits=limits,
                    image_format=image_format,
                    allowed_modes=allowed_modes,
                )
                candidate.verify()

            _validate_container(payload_bytes, image_format)
            with Image.open(io.BytesIO(payload_bytes)) as decoded:
                if str(decoded.format or "").upper() != image_format:
                    _reject("format_changed", "image format changed between validation passes")
                width, height, frames, mode = _decode_all_frames(
                    decoded,
                    limits=limits,
                    image_format=image_format,
                    allowed_modes=allowed_modes,
                    allow_animation=allow_animation,
                )
    except ImageValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError(
            "decompression_bomb",
            "image decompression-bomb limit exceeded",
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageValidationError("invalid_image", "image is invalid or undecodable") from exc

    return ValidatedImage(
        format=image_format,
        extension=extension,
        width=width,
        height=height,
        frames=frames,
        mode=mode,
    )


def validate_image_fd(
    fd: int,
    *,
    limits: ImageValidationLimits,
    expected_suffix: str | None = None,
    format_extensions: Mapping[str, str] = DEFAULT_IMAGE_FORMAT_EXTENSIONS,
    format_suffixes: Mapping[str, Collection[str]] = DEFAULT_IMAGE_FORMAT_SUFFIXES,
    allowed_modes: Mapping[str, Collection[str]] | None = None,
    allow_animation: bool = True,
) -> ValidatedImage:
    """Read at most one byte beyond budget from an already-open file descriptor."""

    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise ImageValidationError("source_unavailable", "image file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        _reject("not_regular_file", "image source is not a regular file")
    if info.st_size > limits.max_bytes:
        _reject("bytes_limit", "image byte limit exceeded")

    original_offset: int | None = None
    try:
        original_offset = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as stream:
            payload = stream.read(limits.max_bytes + 1)
    except OSError as exc:
        raise ImageValidationError("source_read_error", "image file could not be read") from exc
    finally:
        if original_offset is not None:
            try:
                os.lseek(fd, original_offset, os.SEEK_SET)
            except OSError:
                pass

    validated = validate_image_bytes(
        payload,
        limits=limits,
        format_extensions=format_extensions,
        allowed_modes=allowed_modes,
        allow_animation=allow_animation,
    )
    suffix = str(expected_suffix or "").casefold()
    if suffix and suffix not in {
        item.casefold() for item in format_suffixes.get(validated.format, ())
    }:
        _reject("format_mismatch", "image extension does not match its content")
    return validated


def stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def validate_image_path(
    path: Path,
    *,
    limits: ImageValidationLimits,
    require_matching_suffix: bool = True,
    format_extensions: Mapping[str, str] = DEFAULT_IMAGE_FORMAT_EXTENSIONS,
    format_suffixes: Mapping[str, Collection[str]] = DEFAULT_IMAGE_FORMAT_SUFFIXES,
    allowed_modes: Mapping[str, Collection[str]] | None = None,
    allow_animation: bool = True,
) -> ValidatedImage:
    """Safely open and validate a local image without trusting a prior path check."""

    source = Path(path)
    try:
        before = os.lstat(source)
    except OSError as exc:
        raise ImageValidationError("source_unavailable", "image file is unavailable") from exc
    if stat.S_ISLNK(before.st_mode):
        _reject("symlink", "symbolic-link images are not allowed")
    if not stat.S_ISREG(before.st_mode):
        _reject("not_regular_file", "image source is not a regular file")
    if before.st_nlink != 1:
        _reject("hardlink", "hard-linked images are not allowed")
    if before.st_size > limits.max_bytes:
        _reject("bytes_limit", "image byte limit exceeded")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise ImageValidationError("source_open_error", "image file could not be opened") from exc
    try:
        opened = os.fstat(fd)
        current = os.lstat(source)
        expected_identity = stat_identity(before)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_ISLNK(current.st_mode)
            or stat_identity(opened) != expected_identity
            or stat_identity(current) != expected_identity
        ):
            _reject("source_changed", "image source changed while opening")
        validated = validate_image_fd(
            fd,
            limits=limits,
            expected_suffix=source.suffix if require_matching_suffix else None,
            format_extensions=format_extensions,
            format_suffixes=format_suffixes,
            allowed_modes=allowed_modes,
            allow_animation=allow_animation,
        )
        after_fd = os.fstat(fd)
        after_path = os.lstat(source)
        if (
            after_fd.st_nlink != 1
            or stat.S_ISLNK(after_path.st_mode)
            or stat_identity(after_fd) != expected_identity
            or stat_identity(after_path) != expected_identity
        ):
            _reject("source_changed", "image source changed while reading")
        return validated
    finally:
        os.close(fd)


__all__ = [
    "DEFAULT_IMAGE_FORMAT_EXTENSIONS",
    "DEFAULT_IMAGE_FORMAT_SUFFIXES",
    "ImageValidationError",
    "ImageValidationLimits",
    "ValidatedImage",
    "stat_identity",
    "validate_image_bytes",
    "validate_image_fd",
    "validate_image_path",
]
