from __future__ import annotations

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
    ResponseFormatError,
    ResponseLimitError,
)
from core.durable_fanout import load_pending
from core.safe_http import BROWSER_USER_AGENT, SafeHttpResponse
from plugins.earthquake import main as earthquake
from tests.helpers.earthquake_test_support import EARTHQUAKE_TEXT, serve_weibo, weibo_card


def _image_bytes(
    image_format: str,
    *,
    mode: str             = "RGB",
    size: tuple[int, int] = (3, 2),
) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, 1).save(buffer, format=image_format)
    return buffer.getvalue()


def _reported(caplog: pytest.LogCaptureFixture, component: str) -> int:
    """记录到日志里的 public_error 条数（按组件名）。"""

    return sum(
        1
        for record in caplog.records
        if record.name == earthquake.logger.name
        and record.getMessage().startswith("public_error ")
        and f'"component":"{component}"' in record.getMessage()
    )


def _group_context(context, send_result: bool | None) -> None:
    context.default_groups = lambda: [123]
    context.send_action = AsyncMock(return_value=send_result)


@pytest.fixture(autouse=True)
def no_real_weibo(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认不放行真实网络：需要微博响应的测试用 ``serve_weibo`` 覆盖。"""

    def refuse(method: str, url: str, **kwargs):
        del method, kwargs
        raise AssertionError(f"unexpected Weibo request: {url}")

    monkeypatch.setattr(earthquake, "requests_request_bounded", refuse)


@pytest.fixture
def closed_sessions(monkeypatch: pytest.MonkeyPatch) -> list[requests.Session]:
    """记录被关闭的 requests 会话（第三方类上的记录，会话本身仍是真实对象）。"""

    closed: list[requests.Session] = []
    real_close                     = requests.Session.close

    def close(self: requests.Session) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(requests.Session, "close", close)
    return closed


@pytest.fixture
def context(tmp_path: Path):
    return SimpleNamespace(
        data_dir       = tmp_path,
        state          = {},
        secrets        = {},
        request_id     = "earthquake-test",
        default_groups = lambda: [],
        send_action=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_api_pipeline_uses_one_session_and_one_worker_per_scan(
    context,
    monkeypatch: pytest.MonkeyPatch,
    closed_sessions: list[requests.Session],
) -> None:
    weibo       = serve_weibo(monkeypatch, weibo_card())
    main_thread = threading.get_ident()

    result = await earthquake._fetch_earthquake_news(context, force=True)
    second = await earthquake._fetch_earthquake_news(context, force=True)

    calls   = weibo.calls
    session = calls[0][2]
    assert "4.5级地震" in str(result)
    assert "4.5级地震" in str(second)
    assert [call[0] for call in calls] == ["POST", "GET", "GET", "GET"]
    assert len({call[3] for call in calls[:3]}) == 1
    assert all(call[3] != main_thread for call in calls)
    assert isinstance(session, requests.Session)
    assert all(call[2] is session for call in calls)
    assert all(call[4]["redirect_policy"].max_hops == 0 for call in calls)
    visitor_data = calls[0][4]["request_kwargs"]["data"]
    assert visitor_data["ver"] == earthquake._WEIBO_VISITOR_VERSION
    assert visitor_data["rid"] == earthquake._WEIBO_VISITOR_RID
    assert closed_sessions == []
    await earthquake.shutdown(context)
    assert closed_sessions == [session]


@pytest.mark.asyncio
async def test_bootstrap_failures_are_logged_but_index_is_still_attempted(
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    closed_sessions: list[requests.Session],
) -> None:
    weibo = serve_weibo(
        monkeypatch,
        weibo_card(),
        visitor_error  = requests.RequestException("visitor unavailable"),
        config_payload = {"bad": "shape"},
    )

    result = await earthquake._fetch_earthquake_news(context, force=True)

    assert "4.5级地震" in str(result)
    assert len(weibo.calls) == 3
    assert _reported(caplog, "earthquake.bootstrap") == 2
    await earthquake.shutdown(context)
    assert len(closed_sessions) == 1


@pytest.mark.asyncio
async def test_repeated_bootstrap_failures_are_escalated(
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    weibo = serve_weibo(monkeypatch, error=requests.RequestException("visitor unavailable"))

    with caplog.at_level("ERROR", logger="plugins.earthquake.main"):
        for _ in range(3):
            assert await earthquake.scheduled(context) == []

    assert context.state["earthquake_bootstrap_failures"] == 3
    assert "failed consecutively" in caplog.text
    # 索引请求失败后会话被标记为需要重新引导，每次扫描都重新走一遍访客引导。
    assert [call[0] for call in weibo.calls] == ["POST", "GET", "GET"] * 3
    await earthquake.shutdown(context)


@pytest.mark.asyncio
async def test_index_failure_closes_session_and_returns_stable_error(
    context,
    monkeypatch: pytest.MonkeyPatch,
    closed_sessions: list[requests.Session],
) -> None:
    serve_weibo(
        monkeypatch,
        config_payload = {"ok": 1, "data": {}},
        index_error    = requests.RequestException("index unavailable"),
    )

    result = await earthquake._fetch_earthquake_news(context, force=True)

    assert "XQ-PLUGIN-UNEXPECTED" in str(result)
    await earthquake.shutdown(context)
    assert len(closed_sessions) == 1


def test_visitor_script_response_must_be_javascript() -> None:
    assert earthquake._VISITOR_MIME_POLICY.accepts("text/javascript")
    assert not earthquake._VISITOR_MIME_POLICY.accepts("text/html")


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
    first  = {"id": "3"}
    nested = {"id": "2"}
    last   = {"id": "1"}
    cards  = [
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
    target     = context.data_dir / "external-state.json"
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


def test_text_cleaning_removes_hidden_nodes_and_enforces_budget() -> None:
    raw = (
        "<script>secret</script><style>hidden</style>"
        "#地震快讯#<b>中国地震台网正式测定</b>\x00：发生4.0级地震"
    )
    cleaned = earthquake._extract_clean_text(raw)
    assert "secret" not in cleaned
    assert "hidden" not in cleaned
    assert "\x00" not in cleaned

    with pytest.raises(ResponseLimitError, match="raw text"):
        earthquake._extract_clean_text("1" * (earthquake.MAX_RAW_TEXT_CHARS + 1))


def test_prepared_card_keeps_text_but_drops_untrusted_figure_url() -> None:
    for figure_url in (
        "https://example.test/map.jpg",
        "https://[broken",
        "https://user@wx1.sinaimg.cn/map.jpg",
        "https://wx1.sinaimg.cn:444/map.jpg",
    ):
        prepared = earthquake._prepare_mblog(
            {
                "id"          : "42",
                "text"        : EARTHQUAKE_TEXT,
                "original_pic": figure_url,
            }
        )
        assert prepared is not None
        assert prepared.event_id == "42"
        assert prepared.figure_url is None


@pytest.mark.asyncio
async def test_manual_query_does_not_advance_cursor(
    context,
    monkeypatch: pytest.MonkeyPatch,
    closed_sessions: list[requests.Session],
) -> None:
    earthquake._save_since(context, "10")
    serve_weibo(monkeypatch, weibo_card("11"))

    result = await earthquake._fetch_earthquake_news(context, force=True)

    assert result
    assert earthquake._load_since(context) == "10"
    await earthquake.shutdown(context)
    assert len(closed_sessions) == 1


@pytest.mark.asyncio
async def test_manual_query_does_not_read_or_repair_corrupt_cursor(
    context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = context.data_dir / "earthquake.json"
    state_path.write_text("{broken", encoding="utf-8")
    serve_weibo(monkeypatch, weibo_card("11"))

    result = await earthquake._fetch_earthquake_news(context, force=True)

    assert result
    assert state_path.read_text(encoding="utf-8") == "{broken"
    assert not list(context.data_dir.glob("earthquake.json.corrupt-*"))


@pytest.mark.asyncio
async def test_scheduled_scan_keeps_scanning_after_low_magnitude(
    context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earthquake._save_since(context, "198")
    serve_weibo(
        monkeypatch,
        weibo_card("200", magnitude="3.5"),
        weibo_card("199", magnitude="5.0"),
        weibo_card("198"),
    )
    _group_context(context, send_result=False)

    await earthquake.scheduled(context)

    sent = str(context.send_action.await_args.args[0])
    assert "5.0级地震" in sent
    assert "3.5级地震" not in sent
    # 有卡片要投递时，新游标只写在投递记录里，投递确认后才提交为活动游标。
    pending = load_pending(context.data_dir / "earthquake_delivery.json")
    assert pending is not None
    assert pending.commit == {"since_id": "200"}
    assert earthquake._load_since(context) == "198"


def test_feed_selection_scans_sorts_and_deduplicates_out_of_order_events() -> None:
    mblogs = [
        weibo_card("198", magnitude="5.0")["mblog"],
        weibo_card("202", magnitude="5.2")["mblog"],
        weibo_card("200", magnitude="5.0")["mblog"],
        weibo_card("202", magnitude="6.0")["mblog"],
        weibo_card("201", magnitude="5.1")["mblog"],
    ]

    scheduled = earthquake._select_earthquakes(mblogs, since_id="199", force=False)
    manual = earthquake._select_earthquakes(mblogs, since_id="199", force=True)

    assert scheduled.event_ids == ("200", "201", "202")
    assert scheduled.newest_seen_id == "202"
    assert len(scheduled.cards) == 3
    assert manual.event_ids == ("202",)


@pytest.mark.asyncio
async def test_scheduled_pending_cursor_is_committed_only_after_delivery(
    context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earthquake._save_since(context, "100")
    weibo = serve_weibo(monkeypatch, weibo_card("200", magnitude="5.0"))
    _group_context(context, send_result=False)

    await earthquake.scheduled(context)

    pending = load_pending(context.data_dir / "earthquake_delivery.json")
    assert pending is not None
    assert pending.pending_targets()
    assert earthquake._load_since(context) == "100"

    context.send_action = AsyncMock(return_value=True)
    await earthquake.scheduled(context)

    assert earthquake._load_since(context) == "200"
    assert load_pending(context.data_dir / "earthquake_delivery.json") is None
    # 重试直接重发留存的投递记录，不重新抓取。
    assert len(weibo.index_calls) == 1
    context.send_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduled_unknown_delivery_advances_cursor_without_retry(
    context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earthquake._save_since(context, "100")
    serve_weibo(monkeypatch, weibo_card("200", magnitude="5.0"))
    _group_context(context, send_result=None)

    await earthquake.scheduled(context)

    assert earthquake._load_since(context) == "200"
    assert load_pending(context.data_dir / "earthquake_delivery.json") is None
    await earthquake.scheduled(context)
    context.send_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_manual_query_has_bounded_user_message(
    context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_weibo(monkeypatch)

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
    too_wide = _image_bytes("PNG", mode="L", size=(earthquake.MAX_IMAGE_DIMENSION + 1, 1))
    with pytest.raises(ResponseLimitError, match="dimension"):
        earthquake._validate_image_bytes(too_wide, media_type="image/png")

    too_many_pixels = _image_bytes("PNG", mode="L", size=(8_000, 4_001))
    assert 8_000 * 4_001 > earthquake.MAX_IMAGE_PIXELS
    with pytest.raises(ResponseLimitError, match="pixel"):
        earthquake._validate_image_bytes(too_many_pixels, media_type="image/png")

    # 像素上限（3200 万）乘以每像素最多 4 字节仍低于解码上限（128 MiB），
    # 真实图片到不了这个上限，只能临时调低。
    monkeypatch.setattr(earthquake, "MAX_DECODED_IMAGE_BYTES", 15)
    with pytest.raises(ResponseLimitError, match="decoded-size"):
        earthquake._validate_image_bytes(
            _image_bytes("PNG", mode="RGBA", size=(2, 2)), media_type="image/png"
        )


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
    payload = _image_bytes(image_format) + b"POLYGLOT_CANARY"
    with pytest.raises(ResponseFormatError, match="trailing|container"):
        earthquake._validate_image_bytes(payload, media_type=media_type)


@pytest.mark.skipif(not features.check("webp"), reason="Pillow has no WebP support")
def test_animated_webp_is_rejected() -> None:
    buffer = io.BytesIO()
    frames = [Image.new("RGB", (2, 2), color) for color in ("red", "blue")]
    frames[0].save(
        buffer,
        format        = "WEBP",
        save_all      = True,
        append_images = frames[1:],
        duration      = 10,
        loop          = 0,
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
    payload  = _image_bytes("PNG")
    response = SafeHttpResponse(
        url     = "https://wx1.sinaimg.cn/large/map.png",
        status  = 200,
        body    = payload,
        charset = None,
        headers = {"Content-Type": "image/png"},
    )
    # 图片校验在工作线程里解码；记录解码发生的线程。
    worker_threads: list[int] = []
    real_open                 = Image.open

    def tracking_open(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return real_open(*args, **kwargs)

    main_thread = threading.get_ident()
    fetch = AsyncMock(return_value=response)
    with (
        patch.object(earthquake, "fetch_public_bytes", new=fetch),
        patch.object(Image, "open", side_effect=tracking_open),
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
        "User-Agent"     : BROWSER_USER_AGENT,
        "Referer"        : "https://m.weibo.cn/",
        "Accept"         : "image/*",
        "Accept-Encoding": "identity",
    }
    assert not {"Authorization", "Cookie"} & kwargs["headers"].keys()


def test_invalid_image_is_not_persisted(context) -> None:
    response = SafeHttpResponse(
        url     = "https://wx1.sinaimg.cn/map.jpg",
        status  = 200,
        body    = _image_bytes("PNG"),
        charset = None,
        headers = {"Content-Type": "image/jpeg"},
    )
    with pytest.raises(ResponseFormatError):
        earthquake._validate_and_store_figure(context, response)
    assert not [path for path in context.data_dir.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_image_failure_keeps_already_appended_text(
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    figure_url = "https://wx1.sinaimg.cn/map.jpg"
    serve_weibo(monkeypatch, weibo_card("123", magnitude="5.0", image_url=figure_url))
    # 声明为 JPEG 的 PNG 字节：真实的图片校验会拒绝它。
    mismatched = SafeHttpResponse(
        url     = figure_url,
        status  = 200,
        body    = _image_bytes("PNG"),
        charset = None,
        headers = {"Content-Type": "image/jpeg"},
    )
    monkeypatch.setattr(earthquake, "fetch_public_bytes", AsyncMock(return_value=mismatched))

    result = await earthquake._fetch_earthquake_news(context, force=True)

    assert len(result) == 1
    assert result[0]["type"] == "text"
    assert "5.0级地震" in result[0]["data"]["text"]
    assert _reported(caplog, "earthquake.download_image") == 1
    assert not [path for path in context.data_dir.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_scheduled_api_failure_is_not_broadcast_and_keeps_cursor(
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _group_context(context, send_result=True)
    earthquake._save_since(context, "42")
    serve_weibo(monkeypatch, index_error=requests.RequestException("outage-canary"))

    result = await earthquake.scheduled(context)

    assert result == []
    context.send_action.assert_not_awaited()
    assert earthquake._load_since(context) == "42"
    assert _reported(caplog, "earthquake.fetch_scheduled") == 1


@pytest.mark.asyncio
async def test_scheduled_corrupt_cursor_fails_closed_without_raising(
    context,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context.default_groups = lambda: [123]
    (context.data_dir / "earthquake.json").write_text("{broken", encoding="utf-8")
    weibo = serve_weibo(monkeypatch, weibo_card("200", magnitude="5.0"))

    result = await earthquake.scheduled(context)
    again  = await earthquake.scheduled(context)

    assert result == [] and again == []
    context.send_action.assert_not_awaited()
    assert weibo.calls == []
    assert _reported(caplog, "earthquake.fetch_scheduled") >= 1


@pytest.mark.asyncio
async def test_handle_routes_help_latest_and_default(
    context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 低于推送门槛的快讯：只有手动查询（不限震级）才会返回它。
    weibo = serve_weibo(monkeypatch, weibo_card("7", magnitude="3.5"))

    help_result = await earthquake.handle("earthquake", "help", {}, context)
    assert "地震快讯" in str(help_result)
    assert weibo.calls == []

    latest  = await earthquake.handle("earthquake", "latest", {}, context)
    default = await earthquake.handle("earthquake", "", {}, context)

    assert "3.5级地震" in str(latest)
    assert "3.5级地震" in str(default)
    assert len(weibo.index_calls) == 2
    assert not (context.data_dir / "earthquake.json").exists()
    await earthquake.shutdown(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["unknown", "latest extra", '"unterminated', "latest\n"])
async def test_handle_rejects_invalid_commands_without_fetching(
    context,
    monkeypatch: pytest.MonkeyPatch,
    args: str,
) -> None:
    weibo = serve_weibo(monkeypatch, weibo_card())

    result = await earthquake.handle("earthquake", args, {}, context)

    assert result
    assert weibo.calls == []


def test_notification_id_is_order_independent_and_rejects_bad_event_ids() -> None:
    first  = earthquake._notification_id("200", ["002", 1])
    second = earthquake._notification_id("0200", ["1", "2", "2"])
    assert first == second
    with pytest.raises(ValueError, match="event id"):
        earthquake._notification_id("200", [True])
