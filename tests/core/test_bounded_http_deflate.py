"""``Content-Encoding: deflate`` 同时接受 zlib 封装与裸 deflate，限额保持不变。"""

from __future__ import annotations

import zlib

import pytest

from core.bounded_http import (
    BodyLimits,
    ResponseFormatError,
    ResponseLimitError,
    _BoundedDecoder,
)

PAYLOAD = ("<i>弹幕</i>" * 50).encode("utf-8")


def _raw(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _decode(wire: bytes, limits: BodyLimits | None = None, chunk: int = 7) -> bytes:
    decoder = _BoundedDecoder(
        encoding="deflate", limits=limits or BodyLimits(), declared_wire_bytes=None
    )
    for start in range(0, len(wire), chunk):
        decoder.feed(wire[start : start + chunk])
    return decoder.finish()


@pytest.mark.parametrize("chunk", [1, 2, 7, 4096])
def test_both_deflate_forms_decode_across_chunk_boundaries(chunk: int) -> None:
    assert _decode(zlib.compress(PAYLOAD), chunk=chunk) == PAYLOAD
    assert _decode(_raw(PAYLOAD), chunk=chunk) == PAYLOAD


def test_size_limit_applies_to_both_forms() -> None:
    limits = BodyLimits(max_decoded_bytes=100, ratio_grace_bytes=10_000_000)
    for wire in (zlib.compress(PAYLOAD), _raw(PAYLOAD)):
        with pytest.raises(ResponseLimitError):
            _decode(wire, limits)


def test_ratio_limit_applies_to_both_forms() -> None:
    bomb = b"\0" * 5_000_000
    limits = BodyLimits(max_decompression_ratio=5, ratio_grace_bytes=1024)
    for wire in (zlib.compress(bomb, 9), _raw(bomb)):
        with pytest.raises(ResponseLimitError):
            _decode(wire, limits)


def test_garbage_and_truncated_deflate_fail_closed() -> None:
    with pytest.raises(ResponseFormatError):
        _decode(b"not compressed at all, definitely")
    with pytest.raises(ResponseFormatError):
        _decode(_raw(PAYLOAD)[:-6])
    with pytest.raises(ResponseFormatError):
        _decode(b"x")
