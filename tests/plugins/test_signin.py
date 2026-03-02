"""测试signin插件"""

import json
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

ROOT = Path(__file__).resolve().parent.parent.parent

import sys
# Add plugins dir to sys.path so signin can be imported as a package
plugins_dir = str(ROOT / "plugins")
if plugins_dir not in sys.path:
    sys.path.insert(0, plugins_dir)

from signin import main as signin
from signin import yingshi as signin_yingshi
from signin import sony as signin_sony


class MockResponse:
    """模拟HTTP响应"""

    def __init__(self, status_code=200, json_data=None, text_data=None):
        self.status = status_code
        self._json_data = json_data or {}
        self._text_data = text_data or ""

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockHTTPSession:
    """模拟HTTP会话"""

    def __init__(self):
        self._responses = []

    def set_response(self, response):
        self._responses.append(response)

    def get(self, *args, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return MockResponse(json_data={"code": 0, "data": {"checkInId": "test123"}})

    def post(self, *args, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return MockResponse(json_data={"resultData": {"access_token": "test_token"}})


# ============================================================================
# 模块级 fixtures
# ============================================================================

@pytest.fixture
def mock_context():
    """模拟插件上下文"""
    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "signin"
            self.data_dir = self.plugin_dir / "data"
            self.http_session = MockHTTPSession()
            self.secrets = {
                "plugins": {
                    "signin": {
                        "sony": {
                            "login_id": "test_user",
                            "password": "test_pass"
                        },
                        "yingshijufeng": {
                            "app_id": "test_app_id",
                            "kdt_id": "test_kdt_id",
                            "access_token": "test_access_token",
                            "sid": "test_sid"
                        }
                    }
                }
            }

    return MockContext()


@pytest.fixture
def mock_context_no_config():
    """模拟无配置的插件上下文"""
    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "signin"
            self.data_dir = self.plugin_dir / "data"
            self.http_session = MockHTTPSession()
            self.secrets = {
                "plugins": {
                    "signin": {}
                }
            }

    return MockContext()


@pytest.fixture
def mock_context_no_http():
    """模拟无HTTP会话的插件上下文"""
    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "signin"
            self.data_dir = self.plugin_dir / "data"
            self.http_session = None
            self.secrets = {
                "plugins": {
                    "signin": {
                        "sony": {
                            "login_id": "test_user",
                            "password": "test_pass"
                        }
                    }
                }
            }

    return MockContext()


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": "12345",
        "message": "test",
        "message_type": "private"
    }


class TestSigninPlugin:
    """测试signin插件 - 基础测试类"""


class TestShowHelp:
    """测试帮助信息"""

    def test_show_help(self):
        """测试显示帮助信息"""
        help_text = signin._show_help()
        assert help_text is not None
        assert "signin" in help_text.lower()
        assert "sony" in help_text.lower()
        assert "yingshi" in help_text.lower() or "影视" in help_text

    def test_help_contains_commands(self):
        """测试帮助包含命令说明"""
        help_text = signin._show_help()
        assert "/signin" in help_text
        assert "Sony" in help_text or "sony" in help_text


class TestHandleCommand:
    """测试命令处理"""

    @pytest.mark.asyncio
    async def test_handle_empty_args(self, mock_context, mock_event):
        """测试空参数"""
        result = await signin.handle("signin", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_help_command(self, mock_context, mock_event):
        """测试帮助命令"""
        result = await signin.handle("signin", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_chinese_help(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await signin.handle("signin", "帮助", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_question_mark(self, mock_context, mock_event):
        """测试问号帮助"""
        result = await signin.handle("signin", "?", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_unknown_platform(self, mock_context, mock_event):
        """测试未知平台"""
        result = await signin.handle("signin", "unknown_platform", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未知" in result_text or "unknown" in result_text.lower()


class TestSonySignin:
    """测试Sony签到"""

    @pytest.mark.asyncio
    async def test_sony_signin_success(self, mock_context, mock_event):
        """测试Sony签到成功"""
        # 模拟成功的登录和签到响应
        login_response = MockResponse(json_data={
            "resultData": {
                "access_token": "test_token_123"
            }
        })
        sign_response = MockResponse(json_data={
            "resultMsg": [{"message": "签到成功"}]
        })

        mock_context.http_session.set_response(login_response)
        mock_context.http_session.set_response(sign_response)

        result = await signin.handle("signin", "sony", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "Sony" in result_text or "sony" in result_text.lower()

    @pytest.mark.asyncio
    async def test_sony_signin_short_command(self, mock_context, mock_event):
        """测试Sony签到简写命令"""
        login_response = MockResponse(json_data={
            "resultData": {
                "access_token": "test_token_123"
            }
        })
        sign_response = MockResponse(json_data={
            "resultMsg": [{"message": "签到完成"}]
        })

        mock_context.http_session.set_response(login_response)
        mock_context.http_session.set_response(sign_response)

        result = await signin.handle("signin", "s", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_sony_signin_no_config(self, mock_context_no_config, mock_event):
        """测试Sony签到无配置"""
        result = await signin.handle("signin", "sony", mock_event, mock_context_no_config)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未配置" in result_text or "配置" in result_text

    @pytest.mark.asyncio
    async def test_sony_signin_no_http_session(self, mock_context_no_http, mock_event):
        """测试Sony签到无HTTP会话"""
        result = await signin.handle("signin", "sony", mock_event, mock_context_no_http)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "会话" in result_text or "http" in result_text.lower()

    @pytest.mark.asyncio
    async def test_sony_signin_partial_config(self, mock_context, mock_event):
        """测试Sony签到配置不完整"""
        mock_context.secrets["plugins"]["signin"]["sony"] = {
            "login_id": "test_only"
        }

        result = await signin.handle("signin", "sony", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未配置" in result_text or "配置" in result_text


class TestYingshiSignin:
    """测试影视飓风签到"""

    @pytest.mark.asyncio
    async def test_yingshi_signin_success(self, mock_context, mock_event):
        """测试影视飓风签到成功"""
        # 模拟获取签到ID和签到的响应
        checkin_info_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "checkInId": "test_checkin_id_123"
            }
        })
        signin_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "desc": "连续签到3天",
                "times": 5,
                "list": [
                    {
                        "isSuccess": True,
                        "times": "第5天",
                        "infos": {
                            "title": "积分奖励",
                            "desc": "+10积分"
                        }
                    }
                ]
            }
        })

        mock_context.http_session.set_response(checkin_info_response)
        mock_context.http_session.set_response(signin_response)

        result = await signin.handle("signin", "yingshi", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "影视" in result_text or "yingshi" in result_text.lower()

    @pytest.mark.asyncio
    async def test_yingshi_signin_short_command(self, mock_context, mock_event):
        """测试影视飓风签到简写命令"""
        checkin_info_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "checkInId": "test_checkin_id_123"
            }
        })
        signin_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "desc": "签到成功",
                "times": 1,
                "list": []
            }
        })

        mock_context.http_session.set_response(checkin_info_response)
        mock_context.http_session.set_response(signin_response)

        result = await signin.handle("signin", "y", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_yingshi_signin_full_name(self, mock_context, mock_event):
        """测试影视飓风完整名称命令"""
        checkin_info_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "checkInId": "test_checkin_id_456"
            }
        })
        signin_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "desc": "签到成功",
                "times": 2,
                "list": []
            }
        })

        mock_context.http_session.set_response(checkin_info_response)
        mock_context.http_session.set_response(signin_response)

        result = await signin.handle("signin", "yingshijufeng", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_yingshi_signin_no_config(self, mock_context_no_config, mock_event):
        """测试影视飓风签到无配置"""
        result = await signin.handle("signin", "yingshi", mock_event, mock_context_no_config)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未配置" in result_text or "配置" in result_text

    @pytest.mark.asyncio
    async def test_yingshi_signin_no_http_session(self, mock_context_no_http, mock_event):
        """测试影视飓风签到无HTTP会话"""
        # 需要添加yingshijufeng配置才能测试到no http session的分支
        mock_context_no_http.secrets["plugins"]["signin"]["yingshijufeng"] = {
            "app_id": "test_app_id",
            "kdt_id": "test_kdt_id",
            "access_token": "test_access_token",
            "sid": "test_sid"
        }
        result = await signin.handle("signin", "yingshi", mock_event, mock_context_no_http)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "会话" in result_text or "http" in result_text.lower()

    @pytest.mark.asyncio
    async def test_yingshi_signin_partial_config(self, mock_context, mock_event):
        """测试影视飓风签到配置不完整"""
        mock_context.secrets["plugins"]["signin"]["yingshijufeng"] = {
            "app_id": "test_app_id"
        }

        result = await signin.handle("signin", "yingshi", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "未配置" in result_text or "配置" in result_text


class TestYingshiSigninAPIError:
    """测试影视飓风API错误处理"""

    @pytest.mark.asyncio
    async def test_yingshi_checkin_info_error(self, mock_context, mock_event):
        """测试获取签到信息失败"""
        error_response = MockResponse(json_data={
            "code": -1,
            "msg": "获取签到信息失败"
        })

        mock_context.http_session.set_response(error_response)

        result = await signin.handle("signin", "yingshi", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "影视" in result_text or "yingshi" in result_text.lower()

    @pytest.mark.asyncio
    async def test_yingshi_checkin_error(self, mock_context, mock_event):
        """测试执行签到失败"""
        checkin_info_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "checkInId": "test_checkin_id"
            }
        })
        signin_response = MockResponse(json_data={
            "code": -1,
            "msg": "今日已签到"
        })

        mock_context.http_session.set_response(checkin_info_response)
        mock_context.http_session.set_response(signin_response)

        result = await signin.handle("signin", "yingshi", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0


class TestScheduledYingshi:
    """测试定时影视飓风签到"""

    @pytest.mark.asyncio
    async def test_scheduled_yingshi(self, mock_context):
        """测试定时签到函数"""
        checkin_info_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "checkInId": "scheduled_checkin_id"
            }
        })
        signin_response = MockResponse(json_data={
            "code": 0,
            "data": {
                "desc": "定时签到成功",
                "times": 10,
                "list": []
            }
        })

        mock_context.http_session.set_response(checkin_info_response)
        mock_context.http_session.set_response(signin_response)

        result = await signin.scheduled_yingshi(mock_context)
        assert result is not None
        assert len(result) > 0


class TestBuildYingshiHeaders:
    """测试影视飓风请求头构建"""

    def test_build_yingshi_headers(self):
        """测试构建请求头"""
        headers = signin_yingshi._build_headers("test_app_id")
        assert headers is not None
        assert "Host" in headers
        assert "User-Agent" in headers
        assert "test_app_id" in headers.get("Referer", "")

    def test_build_yingshi_headers_contains_required_fields(self):
        """测试请求头包含必需字段"""
        headers = signin_yingshi._build_headers("wx12345")
        assert headers["Host"] == "h5.youzan.com"
        assert headers["content-type"] == "application/json"
        assert "wx12345" in headers["Referer"]


class TestBuildExtraData:
    """测试Extra-Data构建"""

    def test_build_extra_data(self):
        """测试构建Extra-Data"""
        extra_data = signin_yingshi._build_extra_data("test_sid")
        assert extra_data is not None
        assert "is_weapp" in extra_data
        assert "sid" in extra_data
        assert extra_data["sid"] == "test_sid"
        assert "uuid" in extra_data
        assert "ftime" in extra_data

    def test_build_extra_data_uuid_format(self):
        """测试Extra-Data中UUID格式"""
        extra_data = signin_yingshi._build_extra_data("my_sid")
        uuid = extra_data.get("uuid", "")
        assert "xncgEoy8XBh9siy" in uuid
        assert isinstance(extra_data["ftime"], int)


class _RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class TestYingshiTokenTransport:
    def test_get_checkin_id_uses_get_params(self):
        """API 返回 405 on POST，确认使用 GET+params 传递 token"""
        response = MockResponse(json_data={"code": 0, "data": {"checkInId": "cid"}})
        session = _RecordingSession(response)

        ok, checkin_id, _ = asyncio.run(
            signin_yingshi._get_checkin_id(
                session,
                "app",
                "kdt",
                "token123",
                {"X": "1"},
            )
        )

        assert ok is True
        assert checkin_id == "cid"
        assert session.calls
        call = session.calls[0]
        assert "json" not in call
        assert call["params"]["access_token"] == "token123"

    def test_do_checkin_uses_get_params(self):
        """API 返回 405 on POST，确认使用 GET+params 传递 token"""
        response = MockResponse(json_data={"code": 0, "data": {"desc": "ok", "times": 1, "list": []}})
        session = _RecordingSession(response)

        ok, _ = asyncio.run(
            signin_yingshi._do_checkin(
                session,
                "cid",
                "app",
                "kdt",
                "token123",
                {"X": "1"},
            )
        )

        assert ok is True
        assert session.calls
        call = session.calls[0]
        assert "json" not in call
        assert call["params"]["access_token"] == "token123"


class TestGetConfig:
    """测试配置获取"""

    def test_get_config_with_secrets(self, mock_context):
        """测试从context获取影视飓风配置"""
        config = signin_yingshi._get_config(mock_context)
        assert config is not None
        assert "app_id" in config
        assert "kdt_id" in config

    def test_get_config_empty_secrets(self, mock_context_no_config):
        """测试空配置"""
        config = signin_yingshi._get_config(mock_context_no_config)
        assert config is not None
        assert config == {}


class TestSonySigninAPIErrors:
    """测试Sony API错误处理"""

    @pytest.mark.asyncio
    async def test_sony_login_error(self, mock_context, mock_event):
        """测试Sony登录失败"""
        error_response = MockResponse(status_code=401)

        # 创建会话并设置错误响应
        class ErrorSession:
            def post(self, *args, **kwargs):
                error_response.raise_for_status()
                return error_response

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        mock_context.http_session = ErrorSession()

        result = await signin.handle("signin", "sony", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "失败" in result_text or "error" in result_text.lower()


class TestInit:
    """测试插件初始化"""

    def test_init(self):
        """测试插件初始化"""
        signin.init()
        assert True


class TestConstants:
    """测试常量定义"""

    def test_sony_urls(self):
        """测试Sony URL常量"""
        assert hasattr(signin_sony, 'SONY_LOGIN_URL')
        assert hasattr(signin_sony, 'SONY_SIGN_URL')
        assert signin_sony.SONY_LOGIN_URL.startswith("https://")
        assert signin_sony.SONY_SIGN_URL.startswith("https://")

    def test_sony_urls_contain_correct_domain(self):
        """测试Sony URL域名正确"""
        assert "sonystyle.com.cn" in signin_sony.SONY_LOGIN_URL
        assert "sonystyle.com.cn" in signin_sony.SONY_SIGN_URL


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
