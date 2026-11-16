"""媒体读取、缓存和标记解析。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from plugins.xiaoqing_chat.media.event_media_analysis import _load_and_prepare_media_for_llm
from tests.helpers.xiaoqing_chat_media_test_support import (
    PNG_BYTES,
    VISION_GATEWAY,
    LLMError,
    MediaPayloadTooLarge,
    PluginCapabilities,
    RenderedMedia,
    ResolvedMedia,
    find_candidate_by_hint,
    load_emoji_library,
    load_qq_face_catalog,
    make_media_runtime,
    marker_media_part,
    parse_marker,
    raw_media_response,
    read_file_bounded,
    render_event_media_text,
    render_local_media_file,
    resolve_marker,
    run_media_blocking,
    strip_outbound_marker_residue,
    text_without_outbound_marker,
    write_png,
)

mock_context = _fixture_support.mock_context


def _vision_json(
    description: str,
    *,
    kind: str             = "image",
    visible_text: str     = "",
    tags: tuple[str, ...] = (),
):
    """视觉模型的一次成功回答（原始响应形状）。"""

    return raw_media_response(
        json.dumps(
            {
                "kind"                : kind,
                "detailed_description": description,
                "visible_text"        : visible_text,
                "emotion_tags"        : list(tags),
            },
            ensure_ascii=False,
        )
    )


def _scripted_vision(*answers) -> AsyncMock:
    """依次回答每次识图请求，用完重复最后一个。

    条目可以是原始响应、要抛出的异常，或返回前两者的函数 / 协程（用来在模型这条边界上
    做延迟或并发写盘）。
    """

    queue = list(answers)

    async def answer(**_kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            item = item()
            if inspect.isawaitable(item):
                item = await item
        if isinstance(item, BaseException):
            raise item
        return item

    return AsyncMock(side_effect=answer)


def _read_render_cache(context) -> dict:
    cache_path = context.data_dir / "media" / "render_cache.json"
    return json.loads(cache_path.read_text(encoding="utf-8"))["items"]


def _tamper_keeping_mtime(path: Path, text: str, *extra_paths: Path) -> None:
    """改写文件内容并把修改时间还原：缓存若只认修改时间，就看不到这次改动。"""

    tracked = (path, *extra_paths)
    before  = {item: item.stat().st_mtime_ns for item in tracked}
    path.write_text(text, encoding="utf-8")
    for item, mtime_ns in before.items():
        os.utime(item, ns=(mtime_ns, mtime_ns))


@pytest.mark.asyncio
async def test_render_local_media_file_uses_render_cache(mock_context):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "trip_photo.png")

    vision = _scripted_vision(_vision_json("海边落日"))

    with patch(VISION_GATEWAY, new=vision):
        first = await render_local_media_file(image_path, context=mock_context, runtime=runtime)
        second = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert first is not None
    assert second is not None
    assert first.marker == "[图片：海边落日]"
    assert second.marker == "[图片：海边落日]"
    vision.assert_awaited_once()


@pytest.mark.asyncio
async def test_identical_media_analysis_is_single_flight(mock_context):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "same-image.png")

    async def slow_answer():
        await asyncio.sleep(0.03)
        return _vision_json("白色方块")

    vision = _scripted_vision(slow_answer)

    with patch(VISION_GATEWAY, new=vision):
        first, second = await asyncio.gather(
            render_local_media_file(image_path, context=mock_context, runtime=runtime),
            render_local_media_file(image_path, context=mock_context, runtime=runtime),
        )

    assert first is not None and second is not None
    assert first.marker == second.marker == "[图片：白色方块]"
    vision.assert_awaited_once()


def test_bounded_media_read_rejects_oversize_from_stat_before_open(tmp_path: Path):
    image_path = tmp_path / "oversize.png"
    image_path.write_bytes(b"x" * 32)

    with patch.object(Path, "open", side_effect=AssertionError("must reject before read")):
        with pytest.raises(MediaPayloadTooLarge) as raised:
            read_file_bounded(image_path, max_bytes=8)

    assert raised.value.size == 32
    assert raised.value.limit == 8


def test_corrupt_image_payload_is_rejected_before_inbox_write(mock_context):
    from plugins.xiaoqing_chat.media.sources import _materialize_resolved_media

    with pytest.raises(ValueError, match="image"):
        _materialize_resolved_media(
            b"not actually an image",
            segment_type      = "image",
            source_name       = "corrupt.png",
            suffix            = ".png",
            context           = mock_context,
            max_pixels        = 16_000_000,
            max_frames        = 120,
            disk_quota_bytes  = 1024 * 1024,
            cache_ttl_seconds = 3600.0,
        )

    assert list((mock_context.data_dir / "media" / "inbox").glob("*")) == []


def test_image_probe_rejects_decompression_bomb():
    from PIL import Image

    from plugins.xiaoqing_chat.media.loading import inspect_image_payload_details

    # 一千万像素宽高的全白位图：压缩后只有十几 KB，但像素数超过 Pillow 的炸弹阈值。
    # 像素预算放得很宽，拒绝只能来自炸弹防护。
    buffer = BytesIO()
    Image.new("1", (10_000, 10_000)).save(buffer, format="PNG")
    assert 10_000 * 10_000 > Image.MAX_IMAGE_PIXELS

    with pytest.raises(ValueError, match="image") as raised:
        inspect_image_payload_details(buffer.getvalue(), max_pixels=10**9, max_frames=120)

    assert getattr(raised.value.__cause__, "reason", "") == "decompression_bomb"


def test_render_cache_evicts_old_entries_at_fixed_capacity(mock_context):
    from plugins.xiaoqing_chat.media.render_cache import write_render_cache_entry

    cached_path = write_png(mock_context.data_dir / "bounded-cache.png")
    for index in range(6):
        media_hash = f"hash-{index}"
        resolved   = ResolvedMedia(
            media_hash   = media_hash,
            segment_type = "image",
            source_name  = media_hash,
            mime_type    = "image/png",
            cached_path  = cached_path,
            width        = 1,
            height       = 1,
            is_animated  = False,
        )
        rendered = RenderedMedia(
            media_hash   = media_hash,
            kind         = "image",
            description  = f"image {index}",
            emotion_tags = (),
            marker       = f"[图片：image {index}]",
            cached_path  = cached_path,
        )
        write_render_cache_entry(
            mock_context.data_dir,
            resolved,
            rendered,
            source      = "llm",
            quality     = "detailed",
            max_entries = 3,
            max_bytes   = 1024 * 1024,
        )

    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    items = json.loads(cache_path.read_text(encoding="utf-8"))["items"]
    assert list(items) == ["hash-3", "hash-4", "hash-5"]
    assert all(float(item["updated_at"]) > 0 for item in items.values())


def test_render_cache_evicts_entries_until_serialized_size_is_bounded(mock_context):
    from plugins.xiaoqing_chat.media.render_cache import write_render_cache_entry

    cached_path = write_png(mock_context.data_dir / "byte-bounded-cache.png")
    max_bytes   = 1_024
    for index in range(12):
        media_hash = f"large-hash-{index}"
        resolved   = ResolvedMedia(
            media_hash   = media_hash,
            segment_type = "image",
            source_name  = media_hash,
            mime_type    = "image/png",
            cached_path  = cached_path,
            width        = 1,
            height       = 1,
            is_animated  = False,
        )
        rendered = RenderedMedia(
            media_hash   = media_hash,
            kind         = "image",
            description  = f"{index}-" + ("描述" * 120),
            emotion_tags = (),
            marker       = f"[图片：{index}]",
            cached_path  = cached_path,
        )
        write_render_cache_entry(
            mock_context.data_dir,
            resolved,
            rendered,
            source      = "llm",
            quality     = "detailed",
            max_entries = 1_000,
            max_bytes   = max_bytes,
        )

    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    assert cache_path.stat().st_size <= max_bytes
    items = json.loads(cache_path.read_text(encoding="utf-8"))["items"]
    assert len(items) < 12


def test_concurrent_inbox_writes_cannot_race_past_disk_quota(mock_context):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from io import BytesIO

    from PIL import Image

    from core.atomic_store import atomic_write_bytes as real_atomic_write_bytes
    from plugins.xiaoqing_chat.media.sources import _materialize_resolved_media

    def png_bytes(color: str) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
        return buffer.getvalue()

    payloads = [png_bytes("red"), png_bytes("blue")]
    quota    = max(len(payload) for payload in payloads)
    start    = threading.Barrier(len(payloads))

    def materialize(payload: bytes, index: int):
        start.wait(timeout=2)
        return _materialize_resolved_media(
            payload,
            segment_type      = "image",
            source_name       = f"concurrent-{index}.png",
            suffix            = ".png",
            context           = mock_context,
            max_pixels        = 16_000_000,
            max_frames        = 120,
            disk_quota_bytes  = quota,
            cache_ttl_seconds = 3600.0,
        )

    def slow_atomic_write(path: Path, payload: bytes) -> None:
        time.sleep(0.05)
        real_atomic_write_bytes(path, payload)

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources.atomic_write_bytes",
            side_effect=slow_atomic_write,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [
            executor.submit(materialize, payload, index) for index, payload in enumerate(payloads)
        ]
        for future in futures:
            future.result(timeout=3)

    inbox_files = list((mock_context.data_dir / "media" / "inbox").glob("*"))
    assert sum(path.stat().st_size for path in inbox_files) <= quota


@pytest.mark.asyncio
async def test_local_media_applies_pixel_budget_before_analysis(mock_context):
    from PIL import Image

    image_path = mock_context.data_dir / "too_many_pixels.png"
    Image.new("RGB", (2, 2), "white").save(image_path)
    runtime = make_media_runtime(max_image_pixels=1)

    vision = _scripted_vision(AssertionError("oversize image must not reach analysis"))

    with patch(VISION_GATEWAY, new=vision):
        rendered = await render_local_media_file(
            image_path,
            context = mock_context,
            runtime = runtime,
        )

    assert rendered is None
    vision.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_local_media_file_keeps_blocking_read_off_event_loop(mock_context):
    runtime                    = make_media_runtime()
    image_path                 = write_png(mock_context.data_dir / "slow_read.png")
    real_open                  = Path.open
    loop_thread_id             = threading.get_ident()
    read_thread_ids: list[int] = []

    def tracked_open(self: Path, *args, **kwargs):
        if self == image_path:
            read_thread_ids.append(threading.get_ident())
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", tracked_open):
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert read_thread_ids
    assert all(thread_id != loop_thread_id for thread_id in read_thread_ids)
    assert rendered is not None


@pytest.mark.asyncio
async def test_media_blocking_work_has_plugin_local_concurrency_limit():
    release    = threading.Event()
    active     = 0
    peak       = 0
    state_lock = threading.Lock()

    def blocking_operation() -> None:
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        try:
            release.wait(timeout=1)
        finally:
            with state_lock:
                active -= 1

    tasks = [asyncio.create_task(run_media_blocking(blocking_operation)) for _ in range(8)]
    try:
        for _ in range(100):
            if peak == 2:
                break
            await asyncio.sleep(0.005)
        assert peak == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_llm_media_preparation_reads_cached_file_once(mock_context):
    image_path = write_png(mock_context.data_dir / "single_read.png")
    resolved   = ResolvedMedia(
        media_hash   = hashlib.sha256(PNG_BYTES).hexdigest(),
        segment_type = "image",
        source_name  = "single read",
        mime_type    = "image/png",
        cached_path  = image_path,
        width        = 1,
        height       = 1,
        is_animated  = False,
    )

    real_open  = Path.open
    open_count = 0

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_count
        if self == image_path:
            open_count += 1
        return real_open(self, *args, **kwargs)

    with (
        patch.object(Path, "open", counting_open),
        patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("preparation must reuse the bounded read payload"),
        ),
    ):
        prepared, image_b64, source_size = await run_media_blocking(
            _load_and_prepare_media_for_llm,
            resolved,
            max_bytes=1024,
        )
        too_small = await run_media_blocking(
            _load_and_prepare_media_for_llm,
            resolved,
            max_bytes=len(PNG_BYTES) - 1,
        )

    assert prepared is not None
    assert source_size == len(PNG_BYTES)
    assert image_b64
    # 第一次调用只打开文件一次；超过字节上限的第二次在读取前就被拒绝。
    assert open_count == 1
    assert too_small == (None, "", len(PNG_BYTES))


@pytest.mark.asyncio
async def test_render_local_media_file_refreshes_old_fallback_cache_with_explicit_vision(
    mock_context,
):
    runtime            = make_media_runtime()
    image_path         = write_png(mock_context.data_dir / "camera_roll_refresh_probe_longname.png")
    media_hash         = hashlib.sha256(PNG_BYTES).hexdigest()
    cached_description = image_path.stem.replace("_", " ")
    cache_path         = mock_context.data_dir / "media" / "render_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "items": {
                    media_hash: {
                        "kind"        : "image",
                        "description" : cached_description,
                        "emotion_tags": [],
                        "marker"      : f"[图片：{cached_description}]",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    vision = _scripted_vision(_vision_json("一只猫趴在窗边", tags=("慵懒",)))

    with patch(VISION_GATEWAY, new=vision):
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert rendered is not None
    assert rendered.marker == "[图片：一只猫趴在窗边]"
    vision.assert_awaited_once()
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["items"][media_hash]["analysis_source"] == "llm"
    assert saved["items"][media_hash]["description"] == "一只猫趴在窗边"


@pytest.mark.asyncio
async def test_render_local_media_file_keeps_old_fallback_cache_without_explicit_vision(
    mock_context,
):
    runtime = make_media_runtime()
    mock_context.capabilities = PluginCapabilities()
    image_path = write_png(mock_context.data_dir / "camera_roll_refresh_probe_longname.png")
    media_hash = hashlib.sha256(PNG_BYTES).hexdigest()
    cached_description = image_path.stem.replace("_", " ")
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    cached_entry = {
        "kind"        : "image",
        "description" : cached_description,
        "emotion_tags": [],
        "marker"      : f"[图片：{cached_description}]",
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"items": {media_hash: cached_entry}}, ensure_ascii=False),
        encoding="utf-8",
    )
    vision = _scripted_vision(AssertionError("should reuse cache"))

    with patch(VISION_GATEWAY, new=vision):
        rendered = await render_local_media_file(image_path, context=mock_context, runtime=runtime)

    assert rendered is not None
    assert rendered.marker == f"[图片：{cached_description}]"
    vision.assert_not_awaited()
    # 缓存条目原样留着：没有触发重新渲染，也就没有被改写成新的分析来源。
    assert _read_render_cache(mock_context) == {media_hash: cached_entry}


@pytest.mark.asyncio
async def test_render_local_media_file_refreshes_generic_llm_cache_after_prompt_upgrade(
    mock_context,
):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "sticker_refresh_probe.jpg")
    media_hash = hashlib.sha256(PNG_BYTES).hexdigest()
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "items": {
                    media_hash: {
                        "kind"           : "emoji",
                        "description"    : "动画表情",
                        "emotion_tags"   : [],
                        "marker"         : "[表情包：动画表情]",
                        "analysis_source": "llm",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    vision = _scripted_vision(
        _vision_json(
            "一只猫皱着脸，配字是苦鲁西",
            kind = "emoji",
            tags = ("委屈", "难受"),
        )
    )

    with patch(VISION_GATEWAY, new=vision):
        rendered = await render_local_media_file(
            image_path, context=mock_context, runtime=runtime, prefer_emoji=True
        )

    assert rendered is not None
    assert rendered.marker == "[表情包：委屈，难受]"
    vision.assert_awaited_once()
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["items"][media_hash]["analysis_source"] == "llm"
    assert saved["items"][media_hash]["analysis_quality"] == "detailed"
    assert saved["items"][media_hash]["analysis_prompt_version"] >= 3


def test_parse_marker_accepts_first_valid_marker_and_cleans_residue() -> None:
    parsed = parse_marker("哈哈 [想发表情:笑哭] [想发QQ表情:狗头]")

    assert parsed is not None
    assert parsed.kind == "emoji"
    assert parsed.hint == "笑哭"
    assert text_without_outbound_marker("哈哈 [想发表情:笑哭]") == "哈哈"
    assert text_without_outbound_marker("哈哈 [想发表情:笑哭] [想发QQ表情:狗头]") == "哈哈"
    assert strip_outbound_marker_residue("行吧 [想发表情:笑哭") == "行吧"
    assert strip_outbound_marker_residue("行吧[QQ表情：捂脸][图片：猫]") == "行吧"
    assert parse_marker("[想发表情:" + "很长的描述" * 9 + "]") is None


def test_candidate_hint_does_not_treat_number_as_unpublished_list_index() -> None:
    assert find_candidate_by_hint(["猫", "狗"], "1", key_fn=lambda item: (item,)) is None


def test_parse_marker_accepts_rendered_qq_face_marker_as_outbound_fallback() -> None:
    parsed = parse_marker("有点绷不住[QQ表情：捂脸]")

    assert parsed is not None
    assert parsed.kind == "qq_face"
    assert parsed.hint == "捂脸"
    assert text_without_outbound_marker("有点绷不住[QQ表情：捂脸]") == "有点绷不住"


@pytest.mark.asyncio
async def test_resolve_marker_matches_emoji_hint_and_builds_media_part(mock_context):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "media" / "library" / "wuyu.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index_path = image_path.parent / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "file_path"   : "media/library/wuyu.png",
                        "description" : "猫猫无语摊手",
                        "emotion_tags": ["无语", "摊手"],
                        "usage_count" : 0,
                        "last_used_ts": 0.0,
                        "marker"      : "[表情包：猫猫无语摊手]",
                        "status"      : "active",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    parsed = parse_marker("笑死 [想发表情:无语]")
    assert parsed is not None
    resolved = await resolve_marker(parsed, context=mock_context, runtime=runtime)

    assert resolved is not None
    assert resolved.kind == "emoji"
    assert resolved.marker == "[表情包：猫猫无语摊手]"
    part = marker_media_part(mock_context, resolved)
    assert part is not None
    assert part["kind"] == "emoji"
    assert part["media_hash"] == media_hash


@pytest.mark.asyncio
async def test_resolve_marker_matches_qq_face_and_image_hints(mock_context):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "media" / "reply_images" / "猫举手.png")

    face = await resolve_marker(
        parse_marker("来个 [想发QQ表情:狗头]"),
        context = mock_context,
        runtime = runtime,
    )
    image = await resolve_marker(
        parse_marker("看这个 [想发图片:猫举手]"),
        context = mock_context,
        runtime = runtime,
        history = [],
    )

    assert face is not None
    assert face.kind == "qq_face"
    assert face.marker == "[QQ表情：狗头]"
    rendered_face = await resolve_marker(
        parse_marker("行吧[QQ表情：捂脸]"),
        context = mock_context,
        runtime = runtime,
    )
    assert rendered_face is not None
    assert rendered_face.kind == "qq_face"
    assert rendered_face.entry.face_id == "264"
    assert image is not None
    assert image.kind == "image"
    assert image.entry.file_path == image_path
    assert marker_media_part(mock_context, image)["kind"] == "image"


@pytest.mark.asyncio
async def test_resolve_image_marker_rejects_history_path_outside_data_root(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside_path = write_png(tmp_path / "outside.png")
    context = SimpleNamespace(data_dir=data_root)
    history = [
        SimpleNamespace(
            role  = "assistant",
            parts = (
                {
                    "kind"       : "image",
                    "file_path"  : str(outside_path),
                    "description": "outside",
                    "marker"     : "[图片：outside]",
                },
            ),
        )
    ]
    parsed = parse_marker("看看 [想发图片:outside]")

    resolved = await resolve_marker(
        parsed,
        context = context,
        runtime = SimpleNamespace(),
        history = history,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_marker_returns_none_when_candidate_missing(mock_context):
    runtime = make_media_runtime()
    parsed  = parse_marker("哈哈 [想发表情:不存在]")

    assert parsed is not None
    assert await resolve_marker(parsed, context=mock_context, runtime=runtime) is None


@pytest.mark.asyncio
async def test_sticker_marker_naming_a_qq_face_sends_that_face(mock_context):
    runtime = make_media_runtime()
    parsed  = parse_marker("[想发表情:捂脸]")

    assert parsed is not None
    resolved = await resolve_marker(parsed, context=mock_context, runtime=runtime)
    assert resolved is not None and resolved.kind == "qq_face"
    assert "捂脸" in resolved.marker


@pytest.mark.asyncio
async def test_load_emoji_library_uses_mtime_cache(mock_context):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "media" / "library" / "cached.png")
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    (image_path.parent / "index.json").write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "file_path"      : "media/library/cached.png",
                        "description"    : "缓存猫猫",
                        "emotion_tags"   : ["缓存"],
                        "usage_count"    : 0,
                        "last_used_ts"   : 0.0,
                        "marker"         : "[表情包：缓存猫猫]",
                        "status"         : "active",
                        "source"         : "manual",
                        "visibility"     : "global",
                        "global_approved": True,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    index_path = image_path.parent / "index.json"

    first = await load_emoji_library(mock_context, runtime)
    # 库目录和索引的修改时间都没变时，第二次读取不会重新扫描：悄悄改掉索引内容也看不到。
    index_text = index_path.read_text(encoding="utf-8")
    assert "缓存猫猫" in index_text
    _tamper_keeping_mtime(
        index_path,
        index_text.replace("缓存猫猫", "改过的猫猫"),
        image_path.parent,
    )
    second = await load_emoji_library(mock_context, runtime)

    assert [item.media_hash for item in first] == [media_hash]
    assert [item.media_hash for item in second] == [media_hash]
    assert [item.description for item in second] == ["缓存猫猫"]


@pytest.mark.asyncio
async def test_emoji_library_reuses_file_fingerprints_after_index_only_update(mock_context):
    from PIL import Image

    from plugins.xiaoqing_chat.media import emoji_library as library_module

    runtime    = make_media_runtime()
    image_path = mock_context.data_dir / "media" / "library" / "indexed.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1, 1), "red").save(image_path)
    media_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    index_path = image_path.parent / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "entries": {
                    media_hash: {
                        "media_hash"     : media_hash,
                        "file_path"      : "media/library/indexed.png",
                        "description"    : "索引猫猫",
                        "emotion_tags"   : ["开心"],
                        "usage_count"    : 0,
                        "last_used_ts"   : 0.0,
                        "marker"         : "[表情包：索引猫猫]",
                        "status"         : "active",
                        "source"         : "manual",
                        "visibility"     : "global",
                        "global_approved": True,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    await load_emoji_library(mock_context, runtime)
    saved = json.loads(index_path.read_text(encoding="utf-8"))
    assert saved["entries"][media_hash]["file_size"] == image_path.stat().st_size
    assert saved["entries"][media_hash]["file_mtime_ns"] == image_path.stat().st_mtime_ns

    library_module.mark_emoji_used_by_hash(mock_context, media_hash)

    # 图片内容换成另一张同样大小的图，并还原修改时间：大小和修改时间都没变的文件沿用索引里
    # 的 SHA-256 和感知哈希，不会再被读取。若重新哈希，条目会换成别的哈希。
    original_bytes = image_path.read_bytes()
    replacement    = BytesIO()
    Image.new("RGB", (1, 1), "blue").save(replacement, format="PNG")
    assert len(replacement.getvalue()) == len(original_bytes)
    mtime_ns = image_path.stat().st_mtime_ns
    image_path.write_bytes(replacement.getvalue())
    os.utime(image_path, ns=(mtime_ns, mtime_ns))

    event_loop_thread       = threading.get_ident()
    stat_threads: list[int] = []
    real_stat               = Path.stat

    def observed_stat(self: Path, *args, **kwargs):
        if self == image_path.resolve():
            stat_threads.append(threading.get_ident())
        return real_stat(self, *args, **kwargs)

    with patch.object(Path, "stat", observed_stat):
        entries = await load_emoji_library(mock_context, runtime)

    assert [item.media_hash for item in entries] == [media_hash]
    assert entries[0].usage_count == 1
    assert stat_threads and all(thread_id != event_loop_thread for thread_id in stat_threads)


@pytest.mark.asyncio
async def test_load_qq_face_catalog_uses_mtime_cache(mock_context):
    catalog_path = mock_context.data_dir / "media" / "qq_face_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {"entries": {"999": {"labels": ["自定义甲"], "usage_count": 0, "last_used_ts": 0.0}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = await load_qq_face_catalog(mock_context)
    # 文件大小和修改时间都没变时，第二次读取直接用缓存：悄悄改掉文件内容也看不到。
    _tamper_keeping_mtime(
        catalog_path,
        catalog_path.read_text(encoding="utf-8").replace("自定义甲", "自定义乙"),
    )
    second = await load_qq_face_catalog(mock_context)

    assert first
    assert [item.face_id for item in second] == [item.face_id for item in first]
    assert {item.face_id: item.label for item in first}["999"] == "自定义甲"
    assert {item.face_id: item.label for item in second}["999"] == "自定义甲"


@pytest.mark.asyncio
async def test_load_qq_face_catalog_does_not_copy_builtin_catalog_to_data(tmp_path):
    context = SimpleNamespace(data_dir=tmp_path)

    entries = await load_qq_face_catalog(context)

    assert entries
    assert not (tmp_path / "media" / "qq_face_catalog.json").exists()


@pytest.mark.asyncio
async def test_qq_face_catalog_cache_is_bounded(monkeypatch, tmp_path):
    from collections import OrderedDict

    from plugins.xiaoqing_chat.media import qq_face_catalog

    monkeypatch.setattr(qq_face_catalog, "_CATALOG_CACHE", OrderedDict())
    for index in range(qq_face_catalog._CATALOG_CACHE_MAX_ENTRIES + 3):
        await qq_face_catalog.load_qq_face_catalog(
            SimpleNamespace(data_dir=tmp_path / f"context-{index}")
        )

    assert len(qq_face_catalog._CATALOG_CACHE) == qq_face_catalog._CATALOG_CACHE_MAX_ENTRIES
    assert all("context-0" not in key for key in qq_face_catalog._CATALOG_CACHE)


@pytest.mark.asyncio
async def test_render_local_media_file_retries_failed_analysis_on_next_send(mock_context):
    runtime    = make_media_runtime()
    image_path = write_png(mock_context.data_dir / "sticker_retry_probe.jpg")
    media_hash = hashlib.sha256(PNG_BYTES).hexdigest()
    service_up = False

    def answer():
        if not service_up:
            return LLMError("retryable_http_429:busy")
        return _vision_json(
            "一只猫皱着脸，配字是苦鲁西",
            kind = "emoji",
            tags = ("委屈", "难受"),
        )

    vision = _scripted_vision(answer)

    with patch(VISION_GATEWAY, new=vision):
        first = await render_local_media_file(
            image_path,
            context      = mock_context,
            runtime      = runtime,
            prefer_emoji = True,
        )
        failed_entry      = _read_render_cache(mock_context)[media_hash]
        calls_after_first = vision.await_count
        service_up        = True
        second            = await render_local_media_file(
            image_path,
            context      = mock_context,
            runtime      = runtime,
            prefer_emoji = True,
        )

    assert first is not None
    assert first.marker != "[表情包：委屈，难受]"
    assert failed_entry["analysis_source"] == "fallback"
    assert second is not None
    assert second.marker == "[表情包：委屈，难受]"
    assert vision.await_count > calls_after_first
    assert _read_render_cache(mock_context)[media_hash]["analysis_source"] == "llm"


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_in_configured_provider_order(mock_context):
    runtime             = make_media_runtime()
    vision              = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"]   = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4.6v", "glm-4v-flash", "glm-4.1v-thinking-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base"     : "https://open.bigmodel.cn/api/paas/v4",
            "api_key"      : "vision-key",
            "model"        : "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4.6v": {
            "api_base"     : "https://open.bigmodel.cn/api/paas/v4",
            "api_key"      : "vision-key",
            "model"        : "glm-4.6v",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base"     : "https://open.bigmodel.cn/api/paas/v4",
            "api_key"      : "vision-key",
            "model"        : "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4.1v-thinking-flash": {
            "api_base"     : "https://open.bigmodel.cn/api/paas/v4",
            "api_key"      : "vision-key",
            "model"        : "glm-4.1v-thinking-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url"             : "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary"         : "[动画表情]",
                },
            }
        ]
    }
    used_models: list[str] = []

    async def _fake_chat_raw(*, secrets, messages, **kwargs):
        # 实际生效的是候选上固定的 profile，而不是展示用的 model 字段。
        model = secrets["_pinned_model"]
        used_models.append(model)
        if model in {"glm-4.6v-flash", "glm-4.6v"}:
            raise LLMError('retryable_http_429:{"error":{"code":"1305","message":"busy"}}')
        content = messages[1]["content"]
        if isinstance(content, list):
            return raw_media_response(
                json.dumps(
                    {
                        "kind"                : "emoji",
                        "detailed_description": "一只猫皱着脸，配字是苦鲁西",
                        "visible_text"        : "苦鲁西",
                        "emotion_tags"        : ["委屈", "难受"],
                    },
                    ensure_ascii=False,
                )
            )
        return raw_media_response(
            json.dumps(
                {"description": "委屈猫猫苦鲁西", "emotion_tags": ["委屈", "难受"]},
                ensure_ascii=False,
            )
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(PNG_BYTES, "image/png")),
        ),
        patch(
            VISION_GATEWAY,
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"
    assert used_models == [
        "glm-4.6v-flash",
        "glm-4.6v",
        "glm-4v-flash",
    ]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_fallback"' in warning_lines
    assert '"to_provider": "glm-4v-flash"' in warning_lines
