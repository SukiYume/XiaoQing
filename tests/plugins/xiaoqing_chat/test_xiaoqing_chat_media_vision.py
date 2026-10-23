"""视觉分析、重试和降级。"""

from __future__ import annotations

import tests.helpers.xiaoqing_chat_media_test_support as _fixture_support
from tests.helpers.xiaoqing_chat_media_test_support import (
    _PNG_BYTES,
    AsyncMock,
    MediaAnalysisDraft,
    RenderedMedia,
    ResolvedMedia,
    _make_media_runtime,
    _raw_media_response,
    _semantic_retry_reason,
    asyncio,
    json,
    patch,
    pytest,
    render_event_media_text,
)

mock_context = _fixture_support.mock_context


@pytest.mark.asyncio
async def test_render_event_media_text_retries_same_provider_once_on_semantic_failure(mock_context):
    runtime             = _make_media_runtime()
    vision              = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"]   = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4v-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {"message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]}
    used_models: list[str] = []
    call_count = 0

    async def _fake_chat_raw(*, secrets, **kwargs):
        model = secrets["_pinned_model"]
        nonlocal call_count
        used_models.append(model)
        call_count += 1
        if model == "glm-4.6v-flash" and call_count == 1:
            return _raw_media_response('```json\n{"kind":"image","description":"')
        if model == "glm-4.6v-flash" and call_count == 2:
            return _raw_media_response(
                json.dumps({"kind": "image", "description": "海边落日"}, ensure_ascii=False)
            )
        raise AssertionError(f"unexpected fallback provider call: {model}")

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：海边落日]"
    assert used_models == ["glm-4.6v-flash", "glm-4.6v-flash"]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_retry"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_logs_raw_response_metadata(mock_context):
    runtime = _make_media_runtime()
    event   = {"message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]}
    raw_content = json.dumps({"kind": "image", "description": "海边落日"}, ensure_ascii=False)

    async def _fake_chat_raw(**kwargs):
        return (
            {
                "choices": [
                    {
                        "message": {"content": raw_content},
                        "finish_reason": "stop",
                    }
                ]
            },
            "/chat/completions",
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：海边落日]"
    log_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.info.call_args_list
    )
    assert '"step": "media.analyze.detail.ok"' in log_lines
    assert '"used_path": "[redacted ' in log_lines
    assert '"finish_reason": "stop"' in log_lines
    assert f'"raw_chars": {len(raw_content)}' in log_lines


@pytest.mark.asyncio
async def test_render_event_media_text_preserves_visible_text_for_text_heavy_screenshot(
    mock_context,
):
    runtime = _make_media_runtime()
    event   = {
        "message": [{"type": "image", "data": {"url": "https://example.com/news_screenshot.jpg"}}]
    }
    captured: dict[str, object] = {}

    async def _fake_chat_raw(**kwargs):
        messages               = kwargs["messages"]
        captured["prompt"]     = messages[1]["content"][0]["text"]
        captured["max_tokens"] = kwargs["max_tokens"]
        return _raw_media_response(
            json.dumps(
                {
                    "kind": "image",
                    "description": "新闻截图，福岛陆上自卫队射击训练误燃干草引发山火，灭火后下山又遭熊袭击",
                    "visible_text": "22日福岛县陆上自卫队射击训练误燃干草，花5小时扑灭山火，下山时遭熊袭击导致队员受伤",
                    "emotion_tags": ["离谱", "惊险"],
                },
                ensure_ascii=False,
            )
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    prompt = str(captured["prompt"])
    assert "visible_text" in prompt
    assert "只记录画面中可以直接观察或清晰读取的信息" in prompt
    assert "不要把审美评价、幽默效果、文化含义、人物动机或聊天意图写成可见事实" in prompt
    assert all(marker not in prompt for marker in ("例如", "比如", "示例："))
    # 文字为主的截图逐句转写，关键信息照原样保留，界面元素不转写，看不清的字原位标出。
    assert "照原样保留" in prompt
    assert "界面元素" in prompt
    assert "〔看不清〕" in prompt
    assert captured["max_tokens"] >= 800
    assert "射击训练误燃干草" in text
    assert "5小时" in text
    assert "熊袭击导致队员受伤" in text


@pytest.mark.asyncio
async def test_render_event_media_text_retries_when_finish_reason_length(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [{"type": "image", "data": {"url": "https://example.com/news_screenshot.jpg"}}]
    }
    calls                      = 0
    max_tokens_seen: list[int] = []

    async def _fake_chat_raw(**kwargs):
        nonlocal calls
        calls += 1
        max_tokens_seen.append(kwargs["max_tokens"])
        if calls == 1:
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "image",
                        "description": "新闻截图",
                        "visible_text": "陆上自卫队演习引发山火",
                    },
                    ensure_ascii=False,
                ),
                finish_reason="length",
            )
        return _raw_media_response(
            json.dumps(
                {
                    "kind": "image",
                    "description": "新闻截图，福岛陆上自卫队灭火后下山时遭熊袭击，队员受伤",
                    "visible_text": "自卫队训练引发山火，花5小时灭火后遇熊袭击",
                    "emotion_tags": ["意外"],
                },
                ensure_ascii=False,
            )
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert calls == 2
    assert max_tokens_seen == [800, 1600]
    assert "遇熊袭击" in text
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"reason_code": "length_truncated"' in warning_lines
    assert '"step": "media.analyze.provider_retry"' in warning_lines


@pytest.mark.asyncio
async def test_text_on_the_image_is_not_mistaken_for_a_vision_refusal(mock_context):
    """图上写着“无法访问”、带下载参数的网址，都是图的内容；读出来的结果照常采用。"""

    runtime = _make_media_runtime()
    event   = {"message": [{"type": "image", "data": {"url": "https://example.com/notice.png"}}]}
    calls   = 0

    async def _fake_chat_raw(**kwargs):
        nonlocal calls
        calls += 1
        return _raw_media_response(
            json.dumps(
                {
                    "kind": "image",
                    "description": "网页通知截图，蓝色标题栏。",
                    "visible_text": "系统维护通知\n10月25日 0:00-6:00 图书馆数据库无法访问，"
                    "https://lib.example.edu/download?appid=lib 同步停用",
                    "emotion_tags": [],
                },
                ensure_ascii=False,
            )
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert calls == 1
    assert "图书馆数据库无法访问" in text
    assert "download?appid=lib" in text
    assert "网页通知截图，蓝色标题栏，文字内容是“系统维护通知 10月25日" in text


def test_merged_summary_stays_one_bounded_marker():
    from plugins.xiaoqing_chat.constants import MEDIA_SUMMARY_MAX_CHARS
    from plugins.xiaoqing_chat.llm.media_evidence import INBOUND_MEDIA_MARKER_RE
    from plugins.xiaoqing_chat.media.event_media_analysis import _merge_visible_text
    from plugins.xiaoqing_chat.media.markers import build_marker, split_visible_text

    summary = _merge_visible_text("网页通知截图。", "[附件]\n" + "报名截止11月7日。" * 300)

    assert len(summary) <= MEDIA_SUMMARY_MAX_CHARS
    assert not any(char in summary for char in "\n[]")
    assert summary.startswith("网页通知截图，文字内容是“［附件］ 报名截止11月7日。")
    assert summary.endswith("…”")
    assert INBOUND_MEDIA_MARKER_RE.fullmatch(build_marker("image", summary, ()))
    assert split_visible_text(summary)[0] == "网页通知截图"


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_after_semantic_retry_exhausted(mock_context):
    runtime             = _make_media_runtime()
    vision              = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"]   = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4v-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {"message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]}
    used_models: list[str] = []

    async def _fake_chat_raw(*, secrets, **kwargs):
        model = secrets["_pinned_model"]
        used_models.append(model)
        if model == "glm-4.6v-flash":
            return _raw_media_response('```json\n{"kind":"image","description":"')
        return _raw_media_response(
            json.dumps({"kind": "image", "description": "窗边猫猫"}, ensure_ascii=False)
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：窗边猫猫]"
    assert used_models == ["glm-4.6v-flash", "glm-4.6v-flash", "glm-4.6v"]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_retry"' in warning_lines
    assert '"step": "media.analyze.provider_fallback"' in warning_lines
    assert '"to_provider": "glm-4.6v"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_falls_back_immediately_on_request_timeout(mock_context):
    runtime             = _make_media_runtime()
    vision              = mock_context.secrets["plugins"]["xiaoqing_chat"]["vision"]
    vision["default"]   = "glm-4.6v-flash"
    vision["fallbacks"] = ["glm-4v-flash"]
    vision["providers"] = {
        "glm-4.6v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4.6v-flash",
            "endpoint_path": "/chat/completions",
        },
        "glm-4v-flash": {
            "api_base": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "vision-key",
            "model": "glm-4v-flash",
            "endpoint_path": "/chat/completions",
        },
    }
    event = {"message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]}
    used_models: list[str] = []

    async def _fake_chat_raw(*, secrets, **kwargs):
        model = secrets["_pinned_model"]
        used_models.append(model)
        if model == "glm-4.6v-flash":
            raise asyncio.TimeoutError()
        return _raw_media_response(
            json.dumps({"kind": "image", "description": "草地小狗"}, ensure_ascii=False)
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：草地小狗]"
    assert used_models == ["glm-4.6v-flash", "glm-4.6v"]
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.provider_fallback"' in warning_lines
    assert '"to_provider": "glm-4.6v"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_text_uses_detail_when_emoji_refine_is_generic(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "detailed_description": "一只猫皱着脸，配字是苦鲁西",
                        "visible_text": "苦鲁西",
                        "emotion_tags": ["委屈", "难受"],
                    },
                    ensure_ascii=False,
                )
            )
        return _raw_media_response(
            json.dumps({"description": "动画表情", "emotion_tags": []}, ensure_ascii=False)
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：委屈，难受]"


@pytest.mark.asyncio
async def test_render_event_media_text_defers_slow_emoji_refine_and_uses_detail(mock_context):
    runtime = _make_media_runtime(
        enable_emoji_refine_background = True,
        emoji_refine_timeout_seconds   = 0.01,
    )
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "description": "一只黑色猫咪，眼睛瞪得很大",
                        "emotion_tags": ["惊讶", "警觉"],
                    },
                    ensure_ascii=False,
                )
            )
        await asyncio.sleep(60)

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)
        await asyncio.sleep(0.05)

    assert text == "[表情包：惊讶，警觉]"
    warning_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.warning.call_args_list
    )
    assert '"step": "media.analyze.refine.background.timeout"' in warning_lines


@pytest.mark.asyncio
async def test_render_event_media_background_refine_updates_cache(mock_context):
    runtime = _make_media_runtime(
        enable_emoji_refine_background = True,
        emoji_refine_timeout_seconds   = 1.0,
    )
    event = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {
                        "kind": "emoji",
                        "description": "一只黑色猫咪，眼睛瞪得很大",
                        "emotion_tags": ["惊讶", "警觉"],
                    },
                    ensure_ascii=False,
                )
            )
        return _raw_media_response(
            json.dumps(
                {
                    "description": "黑猫瞪大双眼",
                    "emotion_tags": ["惊讶", "警觉", "震惊"],
                },
                ensure_ascii=False,
            )
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)
        await asyncio.sleep(0.05)

    assert text == "[表情包：惊讶，警觉]"
    cache_path = mock_context.data_dir / "media" / "render_cache.json"
    item       = {}
    for _ in range(20):
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        item = next(iter(cache["items"].values()))
        if item.get("description") == "黑猫瞪大双眼":
            break
        await asyncio.sleep(0.01)
    assert item["description"] == "黑猫瞪大双眼"
    assert item["marker"] == "[表情包：惊讶，警觉]"


@pytest.mark.asyncio
async def test_background_refine_does_not_replace_equal_quality_foreground(
    mock_context,
):
    from plugins.xiaoqing_chat.media import event_media_analysis

    runtime = _make_media_runtime(
        enable_emoji_refine_background = True,
        emoji_refine_timeout_seconds   = 1.0,
    )
    resolved = ResolvedMedia(
        media_hash   = "stable-quality",
        segment_type = "image",
        source_name  = "emoji.png",
        mime_type    = "image/png",
        cached_path  = mock_context.data_dir / "emoji.png",
    )
    rendered = RenderedMedia(
        media_hash   = resolved.media_hash,
        kind         = "emoji",
        description  = "一只黑猫瞪大眼睛并举起前爪",
        emotion_tags = ("惊讶", "警觉"),
        marker       = "[表情包：惊讶，警觉]",
        cached_path  = resolved.cached_path,
    )
    refined = MediaAnalysisDraft(
        kind         = "emoji",
        description  = "黑猫震惊",
        visible_text = "",
        emotion_tags = ("惊讶", "警觉"),
        parsed_json  = True,
    )
    pending = []

    def capture_task(_context, coroutine, *, name):
        pending.append(coroutine)

    write_cache = AsyncMock()
    with (
        patch.object(
            event_media_analysis,
            "_refine_emoji_analysis_with_llm",
            new=AsyncMock(return_value=(refined, {})),
        ),
        patch.object(event_media_analysis, "run_media_blocking", new=write_cache),
        patch.object(event_media_analysis, "spawn_bg_task", side_effect=capture_task),
    ):
        event_media_analysis.schedule_background_emoji_refine(
            rendered,
            resolved,
            context = mock_context,
            runtime = runtime,
        )
        await pending[0]

    write_cache.assert_not_awaited()
    logged = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in mock_context.logger.info.call_args_list
    )
    assert '"reason_code": "quality_not_improved"' in logged


@pytest.mark.asyncio
async def test_render_event_media_text_retries_and_then_falls_back_when_detail_empty(mock_context):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/store_emoji.jpg",
                    "emoji_package_id": 1001,
                    "summary": "[动画表情]",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {"kind": "emoji", "description": "", "emotion_tags": []}, ensure_ascii=False
                )
            )
        return _raw_media_response(
            json.dumps({"description": "", "emotion_tags": []}, ensure_ascii=False)
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[表情包：动画表情]"
    log_lines = "\n".join(
        str(call.args[1]) if len(call.args) > 1 else ""
        for call in [
            *mock_context.logger.info.call_args_list,
            *mock_context.logger.warning.call_args_list,
        ]
    )
    assert '"step": "media.analyze.provider_retry"' in log_lines
    assert '"step": "media.analyze.fail"' in log_lines
    assert "semantic_validation_failed" in log_lines
    assert '"step": "media.render.fallback"' in log_lines


@pytest.mark.asyncio
async def test_render_event_media_text_does_not_leak_download_query_into_fallback_label(
    mock_context,
):
    runtime = _make_media_runtime()
    event   = {
        "message": [
            {
                "type": "image",
                "data": {
                    "url": "https://example.com/download?appid=1407&fileid=EhR8SaLBMCs4n",
                },
            }
        ]
    }

    async def _fake_chat_raw(*, messages, **kwargs):
        content = messages[1]["content"]
        if isinstance(content, list):
            return _raw_media_response(
                json.dumps(
                    {"kind": "image", "description": "", "emotion_tags": []}, ensure_ascii=False
                )
            )
        return _raw_media_response(
            json.dumps({"description": "", "emotion_tags": []}, ensure_ascii=False)
        )

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media_analysis.chat_completions_raw_with_fallback_paths",
            new=AsyncMock(side_effect=_fake_chat_raw),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    assert text == "[图片：图片内容暂时无法识别]"


def test_semantic_validation_rejects_vision_refusal_as_a_description(tmp_path):
    resolved = ResolvedMedia(
        media_hash   = "refusal-hash",
        segment_type = "image",
        source_name  = "photo",
        mime_type    = "image/png",
        cached_path  = tmp_path / "photo.png",
    )
    rendered = RenderedMedia(
        media_hash   = "refusal-hash",
        kind         = "image",
        description  = "无法查看这张图片",
        emotion_tags = (),
        marker       = "[图片：无法查看这张图片]",
        cached_path  = resolved.cached_path,
    )
    detail = MediaAnalysisDraft(
        kind         = "image",
        description  = rendered.description,
        visible_text = "",
        emotion_tags = (),
        parsed_json  = True,
    )

    assert (
        _semantic_retry_reason(
            detail                = detail,
            rendered              = rendered,
            used_summary_fallback = False,
            resolved              = resolved,
        )
        == "vision_refusal"
    )


@pytest.mark.asyncio
async def test_render_event_media_text_resolves_remote_url_into_cache(mock_context):
    runtime = _make_media_runtime()
    event   = {"message": [{"type": "image", "data": {"url": "https://example.com/cat_photo.png"}}]}

    with (
        patch(
            "plugins.xiaoqing_chat.media.sources._download_url_bytes",
            new=AsyncMock(return_value=(_PNG_BYTES, "image/png")),
        ),
        patch(
            "plugins.xiaoqing_chat.media.event_media.analyze_media_with_llm",
            new=AsyncMock(return_value=None),
        ),
    ):
        text = await render_event_media_text(event, context=mock_context, runtime=runtime)

    cached_files = list((mock_context.data_dir / "media" / "inbox").glob("*"))
    assert text.startswith("[图片：")
    assert cached_files
