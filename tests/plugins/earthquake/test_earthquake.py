from __future__ import annotations

import ast
import hashlib
import io
import json
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import requests
from PIL import Image, features

from core.bounded_http import (
    BoundedHttpResponse,
    ResponseFormatError,
    ResponseLimitError,
)
from core.safe_http import SafeHttpResponse
from plugins.earthquake import main as earthquake

EARTHQUAKE_TEXT = (
    '#地震快讯#<a href="/123">中国地震台网正式测定</a>'
    '：01月01日00:00在四川发生4.5级地震（ <a href="/location">震源深度10公里</a>）'
)


def _card(
    post_id: str = "1234567890",
    *,
    magnitude: str = "4.5",
    image_url: str | None = None,
) -> dict:
    raw_text = EARTHQUAKE_TEXT.replace("4.5", magnitude)
    mblog = {"id": post_id, "text": raw_text}
    if image_url is not None:
        mblog["original_pic"] = image_url
    return {"mblog": mblog}


def _index(cards: list[dict]) -> dict:
    return {"ok": 1, "data": {"cards": cards}}


def _bounded(url: str, payload: bytes, content_type: str) -> BoundedHttpResponse:
    return BoundedHttpResponse(
        url=url,
        status=200,
        body=payload,
        media_type=content_type,
        charset="utf-8",
        headers={"Content-Type": content_type},
        wire_bytes=len(payload),
        decoded_bytes=len(payload),
    )


def _json_response(url: str, value: object) -> BoundedHttpResponse:
    return _bounded(url, json.dumps(value).encode(), "application/json")


def _image_bytes(
    image_format: str,
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (3, 2),
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, 1).save(buffer, format=image_format)
    return buffer.getvalue()


class TrackingSession:
    def __init__(self) -> None:
        self.entered = False
        self.closed = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def context(tmp_path: Path):
    return SimpleNamespace(
        data_dir=tmp_path,
        state={},
        secrets={},
        request_id="earthquake-test",
        default_groups=lambda: [],
        send_action=AsyncMock(return_value=True),
    )


def test_create_session_only_constructs_session() -> None:
    sentinel = object()
    with patch.object(earthquake.requests, "Session", return_value=sentinel) as constructor:
        assert earthquake._create_session() is sentinel
    constructor.assert_called_once_with()


@pytest.mark.asyncio
async def test_api_pipeline_uses_one_session_and_one_worker_per_scan(context) -> None:
    session = TrackingSession()
    calls: list[tuple[str, str, object, int, dict]] = []

    def bounded(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs["session"], threading.get_ident(), kwargs))
        if "genvisitor2" in url:
            return _bounded(url, b"visitor_gray_callback({});", "text/javascript")
        if url.endswith("/api/config"):
            return _json_response(url, {"ok": 1, "data": {"login": False}})
        return _json_response(url, _index([_card()]))

    main_thread = threading.get_ident()
    with (
        patch.object(earthquake, "_create_session", return_value=session),
        patch.object(earthquake, "requests_request_bounded", side_effect=bounded),
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)
        second = await earthquake._fetch_earthquake_news(context, force=True)

    assert "4.5级地震" in str(result)
    assert "4.5级地震" in str(second)
    assert [call[0] for call in calls] == ["POST", "GET", "GET", "GET"]
    assert len({call[3] for call in calls[:3]}) == 1
    assert all(call[3] != main_thread for call in calls)
    assert all(call[2] is session for call in calls)
    assert all(call[4]["redirect_policy"].max_hops == 0 for call in calls)
    visitor_data = calls[0][4]["request_kwargs"]["data"]
    assert visitor_data["ver"] == earthquake._WEIBO_VISITOR_VERSION
    assert visitor_data["rid"] == earthquake._WEIBO_VISITOR_RID
    assert not session.closed
    await earthquake.shutdown(context)
    assert session.closed


@pytest.mark.asyncio
async def test_bootstrap_failures_are_logged_but_index_is_still_attempted(context) -> None:
    session = TrackingSession()
    calls: list[str] = []

    def bounded(method: str, url: str, **kwargs):
        del method, kwargs
        calls.append(url)
        if "genvisitor2" in url:
            raise requests.RequestException("visitor unavailable")
        if url.endswith("/api/config"):
            return _json_response(url, {"bad": "shape"})
        return _json_response(url, _index([_card()]))

    with (
        patch.object(earthquake, "_create_session", return_value=session),
        patch.object(earthquake, "requests_request_bounded", side_effect=bounded),
        patch.object(earthquake, "public_error_message", return_value="safe") as report,
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)

    assert "4.5级地震" in str(result)
    assert len(calls) == 3
    assert report.call_count == 2
    await earthquake.shutdown(context)
    assert session.closed


def test_repeated_bootstrap_failures_are_escalated(context, caplog) -> None:
    session = TrackingSession()
    with (
        patch.object(
            earthquake,
            "requests_request_bounded",
            side_effect=requests.RequestException("visitor unavailable"),
        ),
        patch.object(earthquake, "public_error_message", return_value="safe"),
        caplog.at_level("ERROR", logger="plugins.earthquake.main"),
    ):
        assert all(not earthquake._bootstrap_session(session, context) for _ in range(3))

    assert context.state["earthquake_bootstrap_failures"] == 3
    assert "failed consecutively" in caplog.text
    earthquake._close_session(session)


@pytest.mark.asyncio
async def test_index_failure_closes_session_and_returns_stable_error(context) -> None:
    session = TrackingSession()

    def bounded(method: str, url: str, **kwargs):
        del method, kwargs
        if "genvisitor2" in url:
            return _bounded(url, b"callback({});", "text/javascript")
        if url.endswith("/api/config"):
            return _json_response(url, {"ok": 1, "data": {}})
        raise requests.RequestException("index unavailable")

    with (
        patch.object(earthquake, "_create_session", return_value=session),
        patch.object(earthquake, "requests_request_bounded", side_effect=bounded),
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)

    assert "XQ-PLUGIN-UNEXPECTED" in str(result)
    await earthquake.shutdown(context)
    assert session.closed


def test_api_limits_and_mime_policies_are_explicit() -> None:
    assert earthquake._VISITOR_BODY_LIMITS.max_wire_bytes == 256 * 1024
    assert earthquake._VISITOR_BODY_LIMITS.max_decoded_bytes == 512 * 1024
    assert earthquake._VISITOR_MIME_POLICY.accepts("text/javascript")
    assert not earthquake._VISITOR_MIME_POLICY.accepts("text/html")
    assert earthquake._INDEX_BODY_LIMITS.max_wire_bytes == 1024 * 1024
    assert earthquake._INDEX_BODY_LIMITS.max_decoded_bytes == 2 * 1024 * 1024
    assert earthquake._INDEX_JSON_LIMITS.max_depth == 24
    assert earthquake._INDEX_JSON_LIMITS.max_nodes == 30_000


@pytest.mark.parametrize(
    "payload,require_cards",
    [
        ([], False),
        ({}, False),
        ({"ok": 0, "data": {}}, False),
        ({"ok": True, "data": {}}, False),
        ({"data": {"cards": []}}, True),
        ({"data": {"cards": {}}}, True),
    ],
)
def test_api_envelope_rejects_malformed_structures(payload, require_cards: bool) -> None:
    with pytest.raises(ResponseFormatError):
        earthquake._validate_api_envelope(payload, require_cards=require_cards)


def test_api_envelope_rejects_excess_cards() -> None:
    payload = {"ok": 1, "data": {"cards": [{}] * (earthquake._MAX_WEIBO_CARDS + 1)}}
    with pytest.raises(ResponseLimitError):
        earthquake._validate_api_envelope(payload, require_cards=True)


@pytest.mark.parametrize(
    "cards",
    [
        [None],
        [{"mblog": []}],
        [{"card_group": {}}],
        [{"card_group": [None]}],
    ],
)
def test_card_expansion_rejects_malformed_nodes(cards: object) -> None:
    with pytest.raises(ResponseFormatError):
        earthquake._iter_mblogs(cards)


def test_card_expansion_applies_one_budget_to_nested_nodes() -> None:
    cards = [{"card_group": [{}] * earthquake._MAX_WEIBO_CARDS}]
    with pytest.raises(ResponseLimitError, match="nested"):
        earthquake._iter_mblogs(cards)


def test_grouped_cards_keep_their_display_order() -> None:
    first = {"id": "3"}
    nested = {"id": "2"}
    last = {"id": "1"}
    cards = [
        {"mblog": first, "card_group": [{"mblog": nested}]},
        {"mblog": last},
    ]
    assert earthquake._iter_mblogs(cards) == [first, nested, last]


def test_state_round_trip_and_corrupt_recovery(context) -> None:
    earthquake._save_since(context, "42")
    assert earthquake._load_since(context) == "42"
    (context.data_dir / "earthquake.json").write_text("{broken", encoding="utf-8")
    assert earthquake._load_since(context) == "42"
    assert json.loads((context.data_dir / "earthquake.json").read_text()) == {"since_id": "42"}
    assert len(list(context.data_dir.glob("earthquake.json.corrupt-*"))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"since_id": "x"},
        {"since_id": -1},
        {"since_id": True},
        {"since_id": 1.0},
        {"since_id": "17", "extra": True},
    ],
)
def test_state_wrong_shape_or_cursor_recovers_checkpoint(context, payload) -> None:
    earthquake._save_since(context, "17")
    state_path = context.data_dir / "earthquake.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert earthquake._load_since(context) == "17"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"since_id": "17"}


def test_state_file_size_is_bounded_before_json_decode(context) -> None:
    earthquake._save_since(context, "23")
    state_path = context.data_dir / "earthquake.json"
    state_path.write_bytes(b"0" * (earthquake.MAX_STATE_BYTES + 1))

    assert earthquake._load_since(context) == "23"
    quarantined = list(context.data_dir.glob("earthquake.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].stat().st_size == earthquake.MAX_STATE_BYTES + 1


def test_state_symlink_is_quarantined_without_touching_target(context) -> None:
    earthquake._save_since(context, "29")
    state_path = context.data_dir / "earthquake.json"
    target = context.data_dir / "external-state.json"
    target.write_text('{"since_id":"999"}', encoding="utf-8")
    state_path.unlink()
    try:
        state_path.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert earthquake._load_since(context) == "29"
    assert target.read_text(encoding="utf-8") == '{"since_id":"999"}'
    assert not state_path.is_symlink()


def test_magnitude_and_clean_text_parsing() -> None:
    clean = earthquake._extract_clean_text(EARTHQUAKE_TEXT)
    assert "发生4.5级地震" in clean
    assert "<a href=" not in clean
    assert earthquake._extract_magnitude(clean) == 4.5
    assert earthquake._extract_magnitude("无震级") is None
    assert earthquake._extract_magnitude("发生999级地震") is None


def test_text_cleaning_removes_hidden_nodes_and_enforces_budget(monkeypatch) -> None:
    raw = (
        "<script>secret</script><style>hidden</style>"
        "#地震快讯#<b>中国地震台网正式测定</b>\x00：发生4.0级地震"
    )
    cleaned = earthquake._extract_clean_text(raw)
    assert "secret" not in cleaned
    assert "hidden" not in cleaned
    assert "\x00" not in cleaned

    monkeypatch.setattr(earthquake, "MAX_RAW_TEXT_CHARS", 3)
    with pytest.raises(ResponseLimitError, match="raw text"):
        earthquake._extract_clean_text("1234")


def test_prepared_card_keeps_text_but_drops_untrusted_figure_url() -> None:
    for figure_url in (
        "https://example.test/map.jpg",
        "https://[broken",
        "https://user@wx1.sinaimg.cn/map.jpg",
        "https://wx1.sinaimg.cn:444/map.jpg",
    ):
        prepared = earthquake._prepare_mblog(
            {
                "id": "42",
                "text": EARTHQUAKE_TEXT,
                "original_pic": figure_url,
            }
        )
        assert prepared is not None
        assert prepared.event_id == "42"
        assert prepared.figure_url is None


@pytest.mark.asyncio
async def test_manual_query_does_not_advance_cursor(context) -> None:
    earthquake._save_since(context, "10")
    session = TrackingSession()
    with (
        patch.object(earthquake, "_create_session", return_value=session),
        patch.object(earthquake, "_bootstrap_session"),
        patch.object(earthquake, "_fetch_weibo", return_value=[_card("11")["mblog"]]),
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)
    assert result
    assert earthquake._load_since(context) == "10"
    await earthquake.shutdown(context)
    assert session.closed


@pytest.mark.asyncio
async def test_manual_query_does_not_read_or_repair_corrupt_cursor(context) -> None:
    state_path = context.data_dir / "earthquake.json"
    state_path.write_text("{broken", encoding="utf-8")
    with (
        patch.object(earthquake, "_create_session", return_value=TrackingSession()),
        patch.object(earthquake, "_bootstrap_session"),
        patch.object(earthquake, "_fetch_weibo", return_value=[_card("11")["mblog"]]),
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)
    assert result
    assert state_path.read_text(encoding="utf-8") == "{broken"
    assert not list(context.data_dir.glob("earthquake.json.corrupt-*"))


@pytest.mark.asyncio
async def test_scheduled_scan_keeps_scanning_after_low_magnitude(context) -> None:
    earthquake._save_since(context, "198")
    cards = [_card("200", magnitude="3.5"), _card("199", magnitude="5.0"), _card("198")]
    with (
        patch.object(earthquake, "_create_session", return_value=TrackingSession()),
        patch.object(earthquake, "_bootstrap_session"),
        patch.object(
            earthquake,
            "_fetch_weibo",
            return_value=[card["mblog"] for card in cards],
        ),
    ):
        result = await earthquake._fetch_earthquake_news(context, force=False)
    assert "5.0级地震" in str(result)
    assert "3.5级地震" not in str(result)
    assert earthquake._load_since(context) == "200"


def test_feed_selection_scans_sorts_and_deduplicates_out_of_order_events() -> None:
    mblogs = [
        _card("198", magnitude="5.0")["mblog"],
        _card("202", magnitude="5.2")["mblog"],
        _card("200", magnitude="5.0")["mblog"],
        _card("202", magnitude="6.0")["mblog"],
        _card("201", magnitude="5.1")["mblog"],
    ]

    scheduled = earthquake._select_earthquakes(mblogs, since_id="199", force=False)
    manual = earthquake._select_earthquakes(mblogs, since_id="199", force=True)

    assert scheduled.event_ids == ("200", "201", "202")
    assert scheduled.newest_seen_id == "202"
    assert len(scheduled.cards) == 3
    assert manual.event_ids == ("202",)


@pytest.mark.asyncio
async def test_scheduled_pending_cursor_is_committed_only_after_delivery(context) -> None:
    context.default_groups = lambda: [123]
    context.send_action = AsyncMock(return_value=False)

    async def fake_fetch(ctx, force=False, advance_cursor=True):
        del force, advance_cursor
        ctx.state["earthquake_pending_since"] = "200"
        return earthquake.segments("M5 event")

    with (
        patch.object(earthquake, "_fetch_earthquake_news", new=fake_fetch),
        patch.object(earthquake, "_save_since") as save,
    ):
        await earthquake.scheduled(context)
    save.assert_not_called()

    context.send_action = AsyncMock(return_value=True)
    with (
        patch.object(earthquake, "_fetch_earthquake_news", new=fake_fetch),
        patch.object(earthquake, "_save_since") as save,
    ):
        await earthquake.scheduled(context)
    save.assert_called_once_with(context, "200")


@pytest.mark.asyncio
async def test_scheduled_unknown_delivery_advances_cursor_without_retry(context) -> None:
    context.default_groups = lambda: [123]
    context.send_action = AsyncMock(return_value=None)

    async def fake_fetch(ctx, force=False, advance_cursor=True):
        del force, advance_cursor
        ctx.state["earthquake_pending_since"] = "200"
        return earthquake.segments("M5 event")

    with (
        patch.object(earthquake, "_fetch_earthquake_news", new=fake_fetch),
        patch.object(earthquake, "_save_since") as save,
    ):
        await earthquake.scheduled(context)

    save.assert_called_once_with(context, "200")


@pytest.mark.asyncio
async def test_empty_manual_query_has_bounded_user_message(context) -> None:
    with (
        patch.object(earthquake, "_create_session", return_value=TrackingSession()),
        patch.object(earthquake, "_bootstrap_session"),
        patch.object(earthquake, "_fetch_weibo", return_value=[]),
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)
    assert "未获取到地震快讯数据" in str(result)


@pytest.mark.parametrize(
    ("image_format", "media_type", "extension"),
    [
        ("JPEG", "image/jpeg", ".jpg"),
        ("PNG", "image/png", ".png"),
        pytest.param(
            "WEBP",
            "image/webp",
            ".webp",
            marks=pytest.mark.skipif(
                not features.check("webp"), reason="Pillow has no WebP support"
            ),
        ),
    ],
)
def test_valid_single_frame_image_formats(
    image_format: str, media_type: str, extension: str
) -> None:
    assert (
        earthquake._validate_image_bytes(
            _image_bytes(image_format),
            media_type=media_type,
        )
        == extension
    )


def test_image_mime_format_mismatch_is_rejected() -> None:
    with pytest.raises(ResponseFormatError):
        earthquake._validate_image_bytes(_image_bytes("PNG"), media_type="image/jpeg")


def test_image_body_and_declared_type_are_bounded() -> None:
    with pytest.raises(ResponseFormatError, match="empty"):
        earthquake._validate_image_bytes(b"", media_type="image/png")
    with pytest.raises(ResponseFormatError, match="MIME"):
        earthquake._validate_image_bytes(b"not-an-image", media_type="image/gif")


def test_image_cache_has_entry_byte_and_age_limits() -> None:
    limits = earthquake._IMAGE_CACHE_LIMITS
    assert limits.max_entries == 64
    assert limits.max_bytes == 64 * 1024 * 1024
    assert limits.ttl_seconds == 30 * 24 * 60 * 60


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-Type": "image/gif"},
        {"Content-Type": "image/jpeg, text/html"},
        {"Content-Type": 123},
    ],
)
def test_image_mime_header_must_be_exact(headers) -> None:
    with pytest.raises(ResponseFormatError):
        earthquake._image_media_type(headers)


def test_image_dimension_pixel_and_decoded_budgets(monkeypatch) -> None:
    payload = _image_bytes("PNG", mode="RGBA", size=(2, 2))
    monkeypatch.setattr(earthquake, "MAX_IMAGE_DIMENSION", 1)
    with pytest.raises(ResponseLimitError, match="dimension"):
        earthquake._validate_image_bytes(payload, media_type="image/png")

    monkeypatch.setattr(earthquake, "MAX_IMAGE_DIMENSION", 16_384)
    monkeypatch.setattr(earthquake, "MAX_IMAGE_PIXELS", 3)
    with pytest.raises(ResponseLimitError, match="pixel"):
        earthquake._validate_image_bytes(payload, media_type="image/png")

    monkeypatch.setattr(earthquake, "MAX_IMAGE_PIXELS", 32_000_000)
    monkeypatch.setattr(earthquake, "MAX_DECODED_IMAGE_BYTES", 15)
    with pytest.raises(ResponseLimitError, match="decoded-size"):
        earthquake._validate_image_bytes(payload, media_type="image/png")


def test_high_bit_depth_png_mode_is_rejected() -> None:
    payload = _image_bytes("PNG", mode="I;16")
    with pytest.raises(ResponseFormatError, match="mode"):
        earthquake._validate_image_bytes(payload, media_type="image/png")


def test_truncated_and_other_format_images_are_rejected() -> None:
    png = _image_bytes("PNG")
    with pytest.raises(ResponseFormatError):
        earthquake._validate_image_bytes(png[:-12], media_type="image/png")
    with pytest.raises(ResponseFormatError):
        earthquake._validate_image_bytes(_image_bytes("GIF"), media_type="image/png")


@pytest.mark.parametrize(
    ("image_format", "media_type"),
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        pytest.param(
            "WEBP",
            "image/webp",
            marks=pytest.mark.skipif(
                not features.check("webp"), reason="Pillow has no WebP support"
            ),
        ),
    ],
)
def test_image_container_rejects_trailing_polyglot_bytes(
    image_format: str,
    media_type: str,
) -> None:
    payload = _image_bytes(image_format) + b"CR222_POLYGLOT_CANARY"
    with pytest.raises(ResponseFormatError, match="trailing|container"):
        earthquake._validate_image_bytes(payload, media_type=media_type)


@pytest.mark.skipif(not features.check("webp"), reason="Pillow has no WebP support")
def test_animated_webp_is_rejected() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
    frames[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=10,
        loop=0,
    )
    with pytest.raises(ResponseFormatError, match="animated"):
        earthquake._validate_image_bytes(buffer.getvalue(), media_type="image/webp")


def test_pillow_decompression_warning_is_an_error() -> None:
    from core import image_validation

    def warn_on_open(*args, **kwargs):
        del args, kwargs
        warnings.warn("bomb", Image.DecompressionBombWarning, stacklevel=2)

    with (
        patch.object(image_validation.Image, "open", side_effect=warn_on_open),
        pytest.raises(ResponseLimitError, match="decompression-bomb"),
    ):
        earthquake._validate_image_bytes(_image_bytes("PNG"), media_type="image/png")


@pytest.mark.asyncio
async def test_download_uses_pinned_public_fetch_minimal_headers_and_worker(context) -> None:
    payload = _image_bytes("PNG")
    response = SafeHttpResponse(
        url="https://wx1.sinaimg.cn/large/map.png",
        status=200,
        body=payload,
        charset=None,
        headers={"Content-Type": "image/png"},
    )
    worker_threads: list[int] = []
    real_store = earthquake._validate_and_store_figure

    def tracking_store(ctx, bounded_response):
        worker_threads.append(threading.get_ident())
        return real_store(ctx, bounded_response)

    main_thread = threading.get_ident()
    fetch = AsyncMock(return_value=response)
    with (
        patch.object(earthquake, "fetch_public_bytes", new=fetch),
        patch.object(earthquake, "_validate_and_store_figure", side_effect=tracking_store),
    ):
        path = await earthquake._download_figure(
            context,
            "https://wx1.sinaimg.cn/large/map.png",
        )

    expected_name = f"{hashlib.sha256(payload).hexdigest()}.png"
    assert path.name == expected_name
    assert path.read_bytes() == payload
    assert worker_threads and worker_threads[0] != main_thread
    kwargs = fetch.await_args.kwargs
    assert kwargs["max_bytes"] == 8 * 1024 * 1024
    assert kwargs["allowed_content_types"] == tuple(earthquake._IMAGE_MIME_FORMATS)
    assert kwargs["allowed_content_type_prefixes"] == ()
    assert kwargs["allowed_hosts"] == {
        "wx1.sinaimg.cn",
        "wx2.sinaimg.cn",
        "wx3.sinaimg.cn",
        "wx4.sinaimg.cn",
    }
    assert kwargs["allowed_schemes"] == ("https",)
    assert kwargs["headers"] == {
        "User-Agent": earthquake._WEIBO_USER_AGENT,
        "Referer": "https://m.weibo.cn/",
        "Accept": "image/*",
        "Accept-Encoding": "identity",
    }
    assert not {"Authorization", "Cookie"} & kwargs["headers"].keys()


def test_invalid_image_is_not_persisted(context) -> None:
    response = SafeHttpResponse(
        url="https://wx1.sinaimg.cn/map.jpg",
        status=200,
        body=_image_bytes("PNG"),
        charset=None,
        headers={"Content-Type": "image/jpeg"},
    )
    with (
        patch.object(earthquake.BoundedFileCache, "put_if_absent") as store,
        pytest.raises(ResponseFormatError),
    ):
        earthquake._validate_and_store_figure(context, response)
    store.assert_not_called()


@pytest.mark.asyncio
async def test_image_failure_keeps_already_appended_text(context) -> None:
    prepared = earthquake._PreparedCard(
        clean_text="中国地震台网正式测定：发生5.0级地震",
        magnitude=5.0,
        figure_url="https://wx1.sinaimg.cn/map.jpg",
        event_id="123",
    )
    with (
        patch.object(
            earthquake,
            "run_sync",
            new=AsyncMock(return_value=earthquake._FetchBatch((prepared,), "0")),
        ),
        patch.object(
            earthquake,
            "_download_figure",
            new=AsyncMock(side_effect=ResponseFormatError("invalid")),
        ),
        patch.object(earthquake, "public_error_message", return_value="safe") as report,
    ):
        result = await earthquake._fetch_earthquake_news(context, force=True)
    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert "5.0级地震" in result[0]["data"]["text"]
    report.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_api_failure_is_not_broadcast_and_keeps_cursor(context) -> None:
    context.default_groups = lambda: [123]
    context.send_action = AsyncMock(return_value=True)
    earthquake._save_since(context, "42")

    with (
        patch.object(
            earthquake,
            "run_sync",
            new=AsyncMock(side_effect=requests.RequestException("outage-canary")),
        ),
        patch.object(earthquake, "public_error_message", return_value="safe") as report,
    ):
        result = await earthquake.scheduled(context)

    assert result == []
    context.send_action.assert_not_awaited()
    assert earthquake._load_since(context) == "42"
    report.assert_called_once()


@pytest.mark.asyncio
async def test_scheduled_corrupt_cursor_fails_closed_without_raising(context) -> None:
    context.default_groups = lambda: [123]
    (context.data_dir / "earthquake.json").write_text("{broken", encoding="utf-8")
    with patch.object(earthquake, "public_error_message", return_value="safe") as report:
        result = await earthquake.scheduled(context)
    assert result == []
    context.send_action.assert_not_awaited()
    assert report.call_count >= 1


@pytest.mark.asyncio
async def test_handle_routes_help_latest_and_default(context) -> None:
    help_result = await earthquake.handle("earthquake", "help", {}, context)
    assert "地震快讯" in str(help_result)
    fetch = AsyncMock(return_value=earthquake.segments("quake"))
    with patch.object(earthquake, "_fetch_earthquake_news", new=fetch):
        await earthquake.handle("earthquake", "latest", {}, context)
        await earthquake.handle("earthquake", "", {}, context)
    assert fetch.await_count == 2
    fetch.assert_awaited_with(context, force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["unknown", "latest extra", '"unterminated', "latest\n"])
async def test_handle_rejects_invalid_commands_without_fetching(context, args: str) -> None:
    fetch = AsyncMock(return_value=earthquake.segments("quake"))
    with patch.object(earthquake, "_fetch_earthquake_news", new=fetch):
        result = await earthquake.handle("earthquake", args, {}, context)
    assert result
    fetch.assert_not_awaited()


def test_notification_id_is_order_independent_and_rejects_bad_event_ids() -> None:
    first = earthquake._notification_id("200", ["002", 1])
    second = earthquake._notification_id("0200", ["1", "2", "2"])
    assert first == second
    with pytest.raises(ValueError, match="event id"):
        earthquake._notification_id("200", [True])


def test_runtime_has_no_direct_unbounded_response_reads() -> None:
    path = Path(earthquake.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden_calls: list[int] = []
    forbidden_reads: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr in {"get", "post", "put", "patch", "delete", "request"}:
            if isinstance(node.value, ast.Name) and node.value.id in {"requests", "session"}:
                forbidden_calls.append(node.lineno)
        if node.attr in {"content", "json", "read", "text", "iter_content"}:
            if isinstance(node.value, ast.Name) and node.value.id in {"response", "resp"}:
                forbidden_reads.append(node.lineno)
    assert not forbidden_calls
    assert not forbidden_reads
    source = path.read_text(encoding="utf-8")
    assert "requests_request_bounded" in source
    assert "fetch_public_bytes" in source
