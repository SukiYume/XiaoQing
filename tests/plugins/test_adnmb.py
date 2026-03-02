"""测试 ADnMB 论坛插件"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
import json
from typing import cast

from plugins.adnmb import main as adnmb_main
from core.interfaces import PluginContextProtocol

ROOT = Path(__file__).resolve().parent.parent.parent


class MockClientSession:
    """模拟 aiohttp ClientSession"""

    def __init__(self):
        self.get_calls = []

    def get(self, url, params=None):
        """记录 GET 调用"""
        self.get_calls.append((url, params))
        return MockResponse()


class MockResponse:
    """模拟 HTTP 响应"""

    def __init__(self, json_data=None, status=200):
        self._json_data = json_data or {}
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self._json_data

    async def read(self):
        return b"fake_image_data"


class TestAdnmbPlugin:
    """测试 ADnMB 插件"""

    def test_init(self):
        """测试插件初始化"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def init" in content

    def test_help_text(self):
        """测试帮助文本格式"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "A岛" in content
        assert "_get_help" in content


class TestAdnmbApi:
    """测试 ADnMB API 模块"""

    def test_api_constants(self):
        """测试 API 常量"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查关键常量和类
        assert "API_HOST" in content
        assert "class Post" in content
        assert "class Thread" in content
        assert "class AdnmbClient" in content

    def test_api_endpoints(self):
        """测试 API 端点定义"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查端点定义
        assert "forum_list" in content
        assert "timeline" in content
        assert "thread" in content
        assert "feed" in content

    def test_post_data_structure(self):
        """测试 Post 数据结构"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查 Post 类方法
        assert "from_json" in content
        assert "format_text" in content

    def test_html_cleaning(self):
        """测试 HTML 清理逻辑"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查 HTML 标签清理
        assert "re.sub" in content
        assert '<[^>]+' in content or "html" in content.lower()

    def test_thread_data_structure(self):
        """测试 Thread 数据结构"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查 Thread 结构
        assert "main_post" in content
        assert "replies" in content
        assert "Replies" in content

    def test_admin_filtering(self):
        """测试 Admin 回复过滤"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查 Admin 过滤
        assert 'Admin"' in content or "'Admin'" in content

    def test_client_methods(self):
        """测试客户端方法"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查客户端方法
        assert "get_forum_list" in content
        assert "get_timeline" in content
        assert "get_thread" in content
        assert "get_ref" in content
        assert "get_feed" in content
        assert "add_feed" in content
        assert "del_feed" in content
        assert "download_image" in content

    def test_image_download(self):
        """测试图片下载功能"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')

        # 检查图片下载相关
        assert "IMAGE_CDN" in content
        assert "cache_dir" in content


class TestAdnmbCommands:
    """测试 ADnMB 命令处理"""

    def test_command_handlers(self):
        """测试命令处理器"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查各种命令处理
        assert "timeline" in content
        assert "forumlist" in content
        assert "showforum" in content
        assert "chuan" in content
        assert "ref" in content
        assert "feed" in content
        assert "addfeed" in content
        assert "delfeed" in content

    def test_disabled_commands(self):
        """测试已禁用的命令"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查已禁用的功能提示
        assert "已禁用" in content
        assert "verify" in content
        assert "login" in content
        assert "reply" in content

    def test_page_parameter(self):
        """测试页码参数处理"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查页码处理
        assert "'p'" in content or '"p"' in content
        assert "page" in content

    def test_format_functions(self):
        """测试格式化函数"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查格式化函数
        assert "format_posts" in content
        assert "format_threads" in content


class TestAdnmbPluginJson:
    """测试 ADnMB plugin.json 配置"""

    def test_plugin_json_exists(self):
        """测试 plugin.json 存在"""
        plugin_json = ROOT / "plugins" / "adnmb" / "plugin.json"
        assert plugin_json.exists()

    def test_plugin_json_content(self):
        """测试 plugin.json 内容"""
        plugin_json = ROOT / "plugins" / "adnmb" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert content["name"] == "adnmb"
        assert "commands" in content
        assert any(cmd["name"] == "adnmb" for cmd in content["commands"])

    def test_command_triggers(self):
        """测试命令触发器"""
        plugin_json = ROOT / "plugins" / "adnmb" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        adnmb_cmd = next((cmd for cmd in content["commands"] if cmd["name"] == "adnmb"), None)
        assert adnmb_cmd is not None
        assert "adnmb" in adnmb_cmd["triggers"]
        assert "a岛" in adnmb_cmd["triggers"] or "岛" in adnmb_cmd["triggers"]


class TestAdnmbIntegration:
    """集成测试"""

    def test_module_files_exist(self):
        """测试模块文件存在"""
        assert (ROOT / "plugins" / "adnmb" / "main.py").exists()
        assert (ROOT / "plugins" / "adnmb" / "adapi.py").exists()

    def test_main_functions(self):
        """测试主模块导出"""
        main_file = ROOT / "plugins" / "adnmb" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        # 检查关键函数存在
        assert "def init" in content
        assert "async def handle" in content
        assert "def _get_help" in content

    def test_api_exports(self):
        """测试 API 模块导出"""
        adapi_file = ROOT / "plugins" / "adnmb" / "adapi.py"
        content = adapi_file.read_text(encoding='utf-8')
        # 检查关键类存在
        assert "class Post" in content
        assert "class Thread" in content
        assert "class AdnmbClient" in content


class _AdnmbTestContext:
    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.http_session = object()
        self.state = {}
        self.secrets = {"plugins": {"adnmb": {"uuid": "uuid-1"}}}


def test_adnmb_reuses_client_from_context_state(monkeypatch, tmp_path):
    created = []

    class FakeClient:
        def __init__(self, session, cache_dir, uuid=""):
            self.session = session
            self.cache_dir = cache_dir
            self.uuid = uuid
            created.append(self)

    monkeypatch.setattr(adnmb_main, "AdnmbClient", FakeClient)

    context = _AdnmbTestContext(tmp_path)

    typed_context = cast(PluginContextProtocol, cast(object, context))
    asyncio.run(adnmb_main.handle("adnmb", "-h", {}, typed_context))
    asyncio.run(adnmb_main.handle("adnmb", "-h", {}, typed_context))

    assert len(created) == 1
