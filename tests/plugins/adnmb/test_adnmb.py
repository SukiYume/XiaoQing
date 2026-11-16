"""测试 ADnMB 论坛插件"""

import asyncio
import json
import time
from pathlib import Path
from typing import ClassVar, cast

import pytest

from core.interfaces import PluginContextProtocol
from plugins.adnmb import adapi as adnmb_adapi
from plugins.adnmb import main as adnmb_main
from plugins.adnmb.adapi import AdnmbClient
from tests.helpers.http import QueuedAiohttpSession, aiohttp_json_response
from tests.helpers.paths import REPOSITORY_ROOT
from tests.helpers.settings_snapshot import with_settings_reader

ROOT = REPOSITORY_ROOT


@pytest.mark.asyncio
async def test_format_posts_limits_concurrent_image_downloads(tmp_path: Path) -> None:
    posts = [
        adnmb_adapi.Post(
            id      = str(index),
            time    = "",
            user_id = "",
            content = f"post-{index}",
            img     = f"{index}.jpg",
        )
        for index in range(5)
    ]
    active  = 0
    maximum = 0

    class _Client:
        async def download_image(self, _image_path: str) -> Path:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return tmp_path / _image_path

    result = await adnmb_main.format_posts(posts, _Client())

    assert maximum <= 3
    assert sum(segment["type"] == "image" for segment in result) == 5


class TestAdnmbRuntimeContract:
    """Exercise the imported plugin and API objects, not source strings."""

    def test_public_entrypoints(self):
        assert callable(adnmb_main.handle) and callable(adnmb_main.shutdown)

    @pytest.mark.asyncio
    async def test_feed_mutation_result_uses_shared_external_text_boundary(
        self,
        tmp_path: Path,
    ) -> None:
        session = QueuedAiohttpSession(
            aiohttp_json_response("\x1b[31m完成\x1b[0m\x00" + "长" * 1_000)
        )
        client = AdnmbClient(session=session, cache_dir=tmp_path, uuid="uuid")

        result = await client.add_feed("123")

        assert "\x1b" not in result and "\x00" not in result
        assert result.endswith("…")
        assert len(result) <= adnmb_adapi.MAX_EXTERNAL_RESULT_CHARS
        assert len(result.encode("utf-8")) <= adnmb_adapi.MAX_EXTERNAL_RESULT_CHARS * 4


class TestAdnmbPluginJson:
    """测试 ADnMB plugin.json 配置"""

    def test_plugin_json_content(self):
        """测试 plugin.json 内容"""
        plugin_json = ROOT / "plugins" / "adnmb" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        assert content["name"] == "adnmb"
        assert "commands" in content
        assert any(cmd["name"] == "adnmb" for cmd in content["commands"])

    def test_command_triggers(self):
        """测试命令触发器"""
        plugin_json = ROOT / "plugins" / "adnmb" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding="utf-8"))

        adnmb_cmd = next((cmd for cmd in content["commands"] if cmd["name"] == "adnmb"), None)
        assert adnmb_cmd is not None
        assert "adnmb" in adnmb_cmd["triggers"]
        assert "a岛" in adnmb_cmd["triggers"] or "岛" in adnmb_cmd["triggers"]


class _AdnmbTestContext:
    def __init__(self, plugin_dir: Path):
        self.plugin_dir   = plugin_dir
        self.http_session = object()
        self.state        = {}
        self.secrets      = {"plugins": {"adnmb": {"uuid": "uuid-1"}}}


@pytest.mark.asyncio
async def test_adnmb_rebuilds_client_when_uuid_changes(tmp_path):
    session = QueuedAiohttpSession(aiohttp_json_response([]), aiohttp_json_response([]))
    context = with_settings_reader(_AdnmbTestContext(tmp_path))
    context.data_dir = tmp_path
    context.http_session = session
    typed_context = cast(PluginContextProtocol, cast(object, context))

    await adnmb_main.handle("adnmb", "-d", {}, typed_context)
    context.secrets["plugins"]["adnmb"]["uuid"] = "uuid-2"
    await adnmb_main.handle("adnmb", "-d", {}, typed_context)

    assert [request[2]["params"]["uuid"] for request in session.requests] == ["uuid-1", "uuid-2"]


def test_adnmb_get_client_reuses_fallback_uuid_client(tmp_path):
    class _Context:
        def __init__(self, plugin_dir: Path):
            self.plugin_dir   = plugin_dir
            self.http_session = object()
            self.state        = {}
            self.secrets      = {"plugins": {"adnmb": {}}}

    context       = with_settings_reader(_Context(tmp_path))
    typed_context = cast(PluginContextProtocol, cast(object, context))
    cache_dir     = context.plugin_dir / "cache"

    first  = adnmb_main._get_client(typed_context, cache_dir)
    second = adnmb_main._get_client(typed_context, cache_dir)

    assert first is second


def test_adnmb_get_client_isolates_subscription_uuid_per_user(tmp_path):
    class _Context:
        def __init__(self, plugin_dir: Path):
            self.plugin_dir      = plugin_dir
            self.http_session    = object()
            self.state           = {}
            self.secrets         = {"plugins": {"adnmb": {"uuid": "shared-uuid"}}}
            self.current_user_id = None

    context       = with_settings_reader(_Context(tmp_path))
    typed_context = cast(PluginContextProtocol, cast(object, context))
    cache_dir     = context.plugin_dir / "cache"

    first = adnmb_main._get_client(typed_context, cache_dir, user_id="1001")
    second = adnmb_main._get_client(typed_context, cache_dir, user_id="1002")

    assert first is not second
    assert first.uuid != second.uuid


def test_adnmb_models_normalize_fields_and_ignore_malformed_replies():
    post   = adnmb_adapi.Post.from_json({"id": 1, "now": 2, "user_hash": 3, "content": None})
    thread = adnmb_adapi.Thread.from_json(
        {
            "id"     : "10",
            "content": "main",
            "Replies": [
                None,
                "bad",
                {"id": "9999999", "content": "special"},
                {"id": 11, "content": "reply"},
            ],
        }
    )

    assert (post.id, post.time, post.user_id, post.content) == ("1", "2", "3", "")
    assert [reply.id for reply in thread.replies] == ["11"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("-m", "请指定板块名称"),
        ("--showforum", "请指定板块名称"),
        ("-c", "请指定串号"),
        ("--chuan", "请指定串号"),
        ("-r", "请指定回复号"),
        ("-a", "请指定要订阅的串号"),
        ("-e", "请指定要取消订阅的串号"),
        ("-p", "请指定页码"),
    ],
)
async def test_adnmb_value_options_reject_missing_values_before_side_effects(
    tmp_path: Path,
    args: str,
    expected: str,
):
    context          = _AdnmbTestContext(tmp_path)
    context.data_dir = tmp_path

    result = await adnmb_main.handle("adnmb", args, {"user_id": 1001}, context)

    assert expected in result[0]["data"]["text"]
    assert "adnmb_runtime" not in context.state
    assert not (tmp_path / "images").exists()


@pytest.mark.asyncio
async def test_adnmb_list_endpoints_ignore_malformed_payload_items(tmp_path):
    session = QueuedAiohttpSession(
        aiohttp_json_response([None, "bad", {"id": 1}]),
        aiohttp_json_response([{"forums": [{"name": "综合版", "id": 1}]}]),
        aiohttp_json_response({"not": "a list"}),
        aiohttp_json_response([None, {"id": 2}]),
    )
    client = AdnmbClient(session=session, cache_dir=tmp_path, uuid="uuid")

    assert [thread.main_post.id for thread in await client.get_timeline()] == ["1"]
    assert await client.get_forum("综合版") == []
    assert [post.id for post in await client.get_feed()] == ["2"]


@pytest.mark.asyncio
async def test_adnmb_client_get_passes_timeout(tmp_path):
    captured = {}

    class _Content:
        async def iter_chunked(self, _size):
            yield b"{}"

    class _Response:
        status                            = 200
        url                               = "https://www.nmbxd1.com/Api/getForumList"
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}
        content_length                    = None
        content                           = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def close(self):
            pass

    class _Session:
        def request(self, _method, _url, **kwargs):
            captured.update(kwargs)
            return _Response()

    client = AdnmbClient(session=_Session(), cache_dir=tmp_path, uuid="uuid")
    await client._get("forum_list")

    assert captured["timeout"] is not None
    assert captured["allow_redirects"] is False
    assert captured["auto_decompress"] is False


def test_adnmb_malformed_html_is_bounded():
    from plugins.adnmb.adapi import Post

    start = time.monotonic()
    post  = Post.from_json({"content": "<" * 500000})
    assert len(post.content) == 65536
    assert time.monotonic() - start < 1
    assert Post.from_json({"content": "a<b>c</b>"}).content == "ac"
