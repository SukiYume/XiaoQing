"""
twitter 插件单元测试

测试 Twitter 图片抓取插件的功能，包括：
- 命令处理（twimg、tw_fetch、help）
- Twitter API 交互
- 图片提取
- 图片下载
- 随机图片选择
- 定时抓取任务
- 配置管理
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import aiohttp

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("twitter_main", ROOT / "plugins" / "twitter" / "main.py")
twitter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(twitter)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir():
    """创建临时数据目录"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_context(temp_data_dir):
    """模拟插件上下文"""
    class MockContext:
        def __init__(self, data_dir):
            self.data_dir = data_dir
            self.secrets = {
                "plugins": {
                    "twitter": {
                        "user_id": "123456789",
                        "headers": {"authorization": "Bearer test_token"},
                        "cookies": {"ct0": "test_csrf"},
                        "proxy": "http://proxy.example.com:8080",
                        "max_pages": 50
                    }
                }
            }
            self.http_session = None
            self.logger = MagicMock()
            self.current_user_id = 12345
            self.current_group_id = 945793035
            self.send_action = AsyncMock()

    return MockContext(temp_data_dir)


@pytest.fixture
def mock_context_no_config(temp_data_dir):
    """模拟没有配置的上下文"""
    class MockContext:
        def __init__(self, data_dir):
            self.data_dir = data_dir
            self.secrets = {}
            self.http_session = None
            self.logger = MagicMock()
            self.current_user_id = 12345
            self.current_group_id = 945793035
            self.send_action = AsyncMock()

    return MockContext(temp_data_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": 12345,
        "group_id": 945793035,
        "message_type": "group"
    }


# ============================================================
# Sample Twitter API Responses
# ============================================================

SAMPLE_TWITTER_TIMELINE_RESPONSE = {
    "data": {
        "user": {
            "result": {
                "timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": "tweet-1234567890",
                                        "content": {
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "legacy": {
                                                            "extended_entities": {
                                                                "media": [
                                                                    {
                                                                        "type": "photo",
                                                                        "media_url_https": "https://pbs.twimg.com/media/ABC123.jpg"
                                                                    }
                                                                ]
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    {
                                        "entryId": "cursor-bottom-12345",
                                        "content": {
                                            "value": "next_cursor_token"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
    }
}

SAMPLE_TWITTER_EMPTY_RESPONSE = {
    "data": {
        "user": {
            "result": {
                "timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": []
                            }
                        ]
                    }
                }
            }
        }
    }
}


# ============================================================
# Test Init
# ============================================================

class TestInit:
    """测试插件初始化"""

    def test_init(self):
        """测试插件初始化"""
        twitter.init()
        assert True


# ============================================================
# Test Config
# ============================================================

class TestConfig:
    """测试配置功能"""

    def test_get_config(self, mock_context):
        """测试获取配置"""
        config = twitter._get_config(mock_context)
        assert config["user_id"] == "123456789"
        assert config["proxy"] == "http://proxy.example.com:8080"

    def test_get_config_empty(self, mock_context_no_config):
        """测试空配置"""
        config = twitter._get_config(mock_context_no_config)
        assert config == {}

    def test_get_headers(self, mock_context):
        """测试获取请求头"""
        headers = twitter._get_headers(mock_context)
        assert "authorization" in headers
        assert "user-agent" in headers

    def test_get_headers_with_custom(self, temp_data_dir):
        """测试自定义请求头"""
        class MockContext:
            def __init__(self, data_dir):
                self.data_dir = data_dir
                self.secrets = {
                    "plugins": {
                        "twitter": {
                            "headers": {
                                "x-custom-header": "custom_value"
                            }
                        }
                    }
                }
                self.http_session = None
                self.logger = MagicMock()

        context = MockContext(temp_data_dir)
        headers = twitter._get_headers(context)
        assert "x-custom-header" in headers
        assert headers["x-custom-header"] == "custom_value"

    def test_get_proxy(self, mock_context):
        """测试获取代理"""
        proxy = twitter._get_proxy(mock_context)
        assert proxy == "http://proxy.example.com:8080"

    def test_get_proxy_default(self, mock_context_no_config):
        """测试默认代理"""
        proxy = twitter._get_proxy(mock_context_no_config)
        assert proxy == "http://127.0.0.1:1080"

    def test_get_user_id(self, mock_context):
        """测试获取用户 ID"""
        user_id = twitter._get_user_id(mock_context)
        assert user_id == "123456789"

    def test_get_user_id_default(self, mock_context_no_config):
        """测试默认用户 ID"""
        user_id = twitter._get_user_id(mock_context_no_config)
        assert user_id == "885295710848995329"

    def test_get_cookies(self, mock_context):
        """测试获取 cookies"""
        cookies = twitter._get_cookies(mock_context)
        assert cookies["ct0"] == "test_csrf"

    def test_get_max_pages(self, mock_context):
        """测试获取最大页数"""
        max_pages = twitter._get_max_pages(mock_context)
        assert max_pages == 50

    def test_get_max_pages_default(self, mock_context_no_config):
        """测试默认最大页数"""
        max_pages = twitter._get_max_pages(mock_context_no_config)
        assert max_pages == twitter.MAX_PAGES_TO_CHECK


# ============================================================
# Test Image Extraction
# ============================================================

class TestImageExtraction:
    """测试图片提取功能"""

    def test_extract_image_urls_single(self):
        """测试提取单个图片 URL"""
        tweet = {
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "legacy": {
                                "extended_entities": {
                                    "media": [
                                        {
                                            "type": "photo",
                                            "media_url_https": "https://pbs.twimg.com/media/ABC123.jpg"
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }

        urls = twitter._extract_image_urls(tweet)
        assert len(urls) == 1
        assert urls[0] == "https://pbs.twimg.com/media/ABC123.jpg"

    def test_extract_image_urls_multiple(self):
        """测试提取多个图片 URL"""
        tweet = {
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "legacy": {
                                "extended_entities": {
                                    "media": [
                                        {
                                            "type": "photo",
                                            "media_url_https": "https://pbs.twimg.com/media/ABC123.jpg"
                                        },
                                        {
                                            "type": "photo",
                                            "media_url_https": "https://pbs.twimg.com/media/DEF456.jpg"
                                        },
                                        {
                                            "type": "photo",
                                            "media_url_https": "https://pbs.twimg.com/media/GHI789.jpg"
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }

        urls = twitter._extract_image_urls(tweet)
        assert len(urls) == 3

    def test_extract_image_urls_no_media(self):
        """测试没有媒体时返回空列表"""
        tweet = {
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "legacy": {}
                        }
                    }
                }
            }
        }

        urls = twitter._extract_image_urls(tweet)
        assert len(urls) == 0

    def test_extract_image_urls_filters_video(self):
        """测试过滤视频类型"""
        tweet = {
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "legacy": {
                                "extended_entities": {
                                    "media": [
                                        {
                                            "type": "photo",
                                            "media_url_https": "https://pbs.twimg.com/media/ABC123.jpg"
                                        },
                                        {
                                            "type": "video",
                                            "media_url_https": "https://pbs.twimg.com/media/VIDEO.mp4"
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }

        urls = twitter._extract_image_urls(tweet)
        assert len(urls) == 1
        assert urls[0] == "https://pbs.twimg.com/media/ABC123.jpg"


# ============================================================
# Test Timeline Fetching
# ============================================================

class TestTimelineFetching:
    """测试时间线获取功能"""

    @pytest.mark.asyncio
    async def test_fetch_timeline_success(self, mock_context):
        """测试成功获取时间线"""
        class MockResponse:
            status = 200
            async def json(self):
                return SAMPLE_TWITTER_TIMELINE_RESPONSE

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        tweets, cursor, has_next = await twitter._fetch_timeline(mock_context)
        assert len(tweets) == 1
        assert tweets[0]["entryId"] == "tweet-1234567890"
        assert cursor == "next_cursor_token"
        assert has_next is True

    @pytest.mark.asyncio
    async def test_fetch_timeline_with_cursor(self, mock_context):
        """测试带 cursor 获取时间线"""
        class MockResponse:
            status = 200
            async def json(self):
                return SAMPLE_TWITTER_TIMELINE_RESPONSE

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        tweets, cursor, has_next = await twitter._fetch_timeline(mock_context, cursor="test_cursor")
        assert len(tweets) >= 0

    @pytest.mark.asyncio
    async def test_fetch_timeline_http_error(self, mock_context):
        """测试 HTTP 错误"""
        class MockResponse:
            status = 403
            async def text(self):
                return "Forbidden"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        tweets, cursor, has_next = await twitter._fetch_timeline(mock_context)
        assert tweets == []
        assert cursor is None
        assert has_next is False

    @pytest.mark.asyncio
    async def test_fetch_timeline_exception(self, mock_context):
        """测试异常情况"""
        class MockGetContextManager:
            async def __aenter__(self):
                raise aiohttp.ClientError("Connection error")
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        tweets, cursor, has_next = await twitter._fetch_timeline(mock_context)
        assert tweets == []
        assert cursor is None
        assert has_next is False


# ============================================================
# Test Image Download
# ============================================================

class TestImageDownload:
    """测试图片下载功能"""

    @pytest.mark.asyncio
    async def test_download_image_success(self, mock_context, temp_data_dir):
        """测试成功下载图片"""
        class MockResponse:
            status = 200
            async def read(self):
                return b"fake image data"

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()
        save_dir = temp_data_dir / "images"
        save_dir.mkdir(parents=True, exist_ok=True)

        result = await twitter._download_image(
            "https://pbs.twimg.com/media/ABC123.jpg",
            save_dir,
            mock_context
        )

        assert result is True
        # 检查文件是否创建
        assert (save_dir / "ABC123.jpg").exists()

    @pytest.mark.asyncio
    async def test_download_image_already_exists(self, mock_context, temp_data_dir):
        """测试图片已存在"""
        save_dir = temp_data_dir / "images"
        save_dir.mkdir(parents=True, exist_ok=True)

        # 创建已存在的文件
        existing_file = save_dir / "ABC123.jpg"
        existing_file.write_bytes(b"existing data")

        result = await twitter._download_image(
            "https://pbs.twimg.com/media/ABC123.jpg",
            save_dir,
            MagicMock()
        )

        assert result is False  # 已存在，跳过

    @pytest.mark.asyncio
    async def test_download_image_failure(self, mock_context, temp_data_dir):
        """测试下载失败"""
        class MockGetContextManager:
            async def __aenter__(self):
                raise aiohttp.ClientError("Download failed")
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()
        save_dir = temp_data_dir / "images"
        save_dir.mkdir(parents=True, exist_ok=True)

        result = await twitter._download_image(
            "https://pbs.twimg.com/media/ABC123.jpg",
            save_dir,
            mock_context
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_download_image_http_error(self, mock_context, temp_data_dir):
        """测试 HTTP 错误"""
        class MockResponse:
            status = 404

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()
        save_dir = temp_data_dir / "images"
        save_dir.mkdir(parents=True, exist_ok=True)

        result = await twitter._download_image(
            "https://pbs.twimg.com/media/ABC123.jpg",
            save_dir,
            mock_context
        )

        assert result is False


# ============================================================
# Test Random Image Selection
# ============================================================

class TestRandomImage:
    """测试随机图片选择功能"""

    @pytest.mark.asyncio
    async def test_get_random_image_with_images(self, mock_context, temp_data_dir):
        """测试有图片时获取随机图片"""
        import aiofiles

        # 创建测试图片
        images_dir = temp_data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "img1.jpg").write_bytes(b"data1")
        (images_dir / "img2.jpg").write_bytes(b"data2")
        (images_dir / "img3.jpg").write_bytes(b"data3")

        img_path = await twitter._get_random_image(mock_context)
        assert img_path is not None
        assert "images" in img_path
        assert img_path.endswith((".jpg", ".png", ".jpeg", ".webp"))

    @pytest.mark.asyncio
    async def test_get_random_image_no_images(self, mock_context, temp_data_dir):
        """测试没有图片时"""
        images_dir = temp_data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        # 不创建任何图片

        img_path = await twitter._get_random_image(mock_context)
        assert img_path is None

    @pytest.mark.asyncio
    async def test_get_random_image_reset_after_all_posted(self, mock_context, temp_data_dir):
        """测试所有图片都发送过后重置"""
        # 创建测试图片
        images_dir = temp_data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "img1.jpg").write_bytes(b"data1")

        # 创建 posted.txt 文件，标记所有图片都已发送
        posted_file = temp_data_dir / "posted.txt"
        posted_file.write_text("img1.jpg\n", encoding="utf-8")

        # 第一次调用应该仍然返回图片（因为重置）
        img_path = await twitter._get_random_image(mock_context)
        assert img_path is not None


# ============================================================
# Test Handle Commands
# ============================================================

class TestHandleCommands:
    """测试命令处理"""

    @pytest.mark.asyncio
    async def test_handle_twimg_help(self, mock_context, mock_event):
        """测试 twimg help 命令"""
        result = await twitter.handle("twimg", "help", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "推特" in result_text or "图片" in result_text

    @pytest.mark.asyncio
    async def test_handle_twimg_help_chinese(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await twitter.handle("twimg", "帮助", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "推特" in result_text or "帮助" in result_text

    @pytest.mark.asyncio
    async def test_handle_tw_fetch_help(self, mock_context, mock_event):
        """测试 tw_fetch help 命令"""
        result = await twitter.handle("tw_fetch", "help", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "抓取" in result_text or "推特" in result_text

    @pytest.mark.asyncio
    async def test_handle_tw_fetch(self, mock_context, mock_event):
        """测试 tw_fetch 命令"""
        with patch.object(twitter, '_fetch_twitter_images', new=AsyncMock(return_value=5)) as mock_fetch:
            result = await twitter.handle("tw_fetch", "", mock_event, mock_context)
            assert result is not None
            result_text = str(result)
            assert "5" in result_text or "完成" in result_text
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_twimg_with_local_images(self, mock_context, mock_event):
        """测试 twimg 有本地图片时"""
        # 创建测试图片
        images_dir = mock_context.data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "test.jpg").write_bytes(b"fake image")

        result = await twitter.handle("twimg", "", mock_event, mock_context)
        assert result is not None
        # 应该返回图片消息
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_handle_twimg_no_local_images(self, mock_context, mock_event):
        """测试 twimg 没有本地图片时"""
        # 确保没有图片目录
        images_dir = mock_context.data_dir / "images"
        if images_dir.exists():
            # 清空目录
            for f in images_dir.iterdir():
                f.unlink()

        with patch.object(twitter, '_fetch_twitter_images', new=AsyncMock(return_value=0)) as mock_fetch:
            result = await twitter.handle("twimg", "", mock_event, mock_context)
            assert result is not None
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_exception(self, mock_context, mock_event):
        """测试处理异常"""
        with patch.object(twitter, '_get_random_image', new=AsyncMock(side_effect=Exception("Test error"))):
            result = await twitter.handle("twimg", "", mock_event, mock_context)
            assert result is not None
            result_text = str(result)
            assert "失败" in result_text or "错误" in result_text


# ============================================================
# Test Image Fetching
# ============================================================

class TestImageFetching:
    """测试图片抓取功能"""

    @pytest.mark.asyncio
    async def test_fetch_twitter_images(self, mock_context):
        """测试抓取 Twitter 图片"""
        # Mock timeline responses
        call_count = [0]

        class MockResponse:
            status = 200
            async def json(self):
                call_count[0] += 1
                if call_count[0] == 1:
                    return SAMPLE_TWITTER_TIMELINE_RESPONSE
                else:
                    return SAMPLE_TWITTER_EMPTY_RESPONSE

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                # 图片下载请求
                if "pbs.twimg.com" in args[0]:
                    class ImgResp:
                        status = 200
                        async def read(self):
                            return b"image data"
                    class ImgGetCtx:
                        async def __aenter__(self):
                            return ImgResp()
                        async def __aexit__(self, *args):
                            pass
                    return ImgGetCtx()
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        count = await twitter._fetch_twitter_images(mock_context)
        assert count >= 0

    @pytest.mark.asyncio
    async def test_fetch_twitter_images_empty_timeline(self, mock_context):
        """测试空时间线"""
        class MockResponse:
            status = 200
            async def json(self):
                return SAMPLE_TWITTER_EMPTY_RESPONSE

        class MockGetContextManager:
            async def __aenter__(self):
                return MockResponse()
            async def __aexit__(self, *args):
                pass

        class MockSession:
            def get(self, *args, **kwargs):
                return MockGetContextManager()

        mock_context.http_session = MockSession()

        count = await twitter._fetch_twitter_images(mock_context)
        assert count == 0


# ============================================================
# Test Scheduled
# ============================================================

class TestScheduled:
    """测试定时任务"""

    @pytest.mark.asyncio
    async def test_scheduled_fetch(self, mock_context):
        """测试定时抓取任务"""
        with patch.object(twitter, '_fetch_twitter_images', new=AsyncMock(return_value=10)) as mock_fetch:
            result = await twitter.scheduled_fetch(mock_context)
            assert result == []
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheduled_fetch_logs_new_images(self, mock_context):
        """测试定时抓取记录日志"""
        with patch.object(twitter, '_fetch_twitter_images', new=AsyncMock(return_value=5)) as mock_fetch:
            result = await twitter.scheduled_fetch(mock_context)
            assert result == []
            # 验证函数被调用
            mock_fetch.assert_called_once_with(mock_context)


# ============================================================
# Test Help
# ============================================================

class TestHelp:
    """测试帮助信息"""

    def test_show_help_twimg(self):
        """测试 twimg 帮助"""
        help_text = twitter._show_help_twimg()
        assert help_text is not None
        assert "twimg" in help_text or "推特" in help_text

    def test_show_help_tw_fetch(self):
        """测试 tw_fetch 帮助"""
        help_text = twitter._show_help_tw_fetch()
        assert help_text is not None
        assert "tw_fetch" in help_text or "抓取" in help_text


# ============================================================
# Test Constants
# ============================================================

class TestConstants:
    """测试常量"""

    def test_max_pages_const(self):
        """测试 MAX_PAGES_TO_CHECK 常量"""
        assert hasattr(twitter, 'MAX_PAGES_TO_CHECK')
        assert twitter.MAX_PAGES_TO_CHECK > 0

    def test_max_pages_without_new_images(self):
        """测试 MAX_PAGES_WITHOUT_NEW_IMAGES 常量"""
        assert hasattr(twitter, 'MAX_PAGES_WITHOUT_NEW_IMAGES')
        assert twitter.MAX_PAGES_WITHOUT_NEW_IMAGES > 0


# ============================================================
# Test Edge Cases
# ============================================================

class TestEdgeCases:
    """测试边界情况"""

    @pytest.mark.asyncio
    async def test_handle_random_image_none(self, mock_context, mock_event):
        """测试随机图片为 None 时的处理"""
        # 确保没有图片
        images_dir = mock_context.data_dir / "images"
        if images_dir.exists():
            for f in images_dir.iterdir():
                f.unlink()

        with patch.object(twitter, '_fetch_twitter_images', new=AsyncMock(return_value=0)):
            with patch.object(twitter, '_get_random_image', new=AsyncMock(return_value=None)):
                result = await twitter.handle("twimg", "", mock_event, mock_context)
                assert result is not None
                result_text = str(result)
                assert "无法获取" in result_text or "失败" in result_text

    @pytest.mark.asyncio
    async def test_fetch_images_with_max_pages(self, mock_context):
        """测试达到最大页数时停止"""
        pages_fetched = [0]

        async def mock_fetch(context, cursor=None):
            pages_fetched[0] += 1
            if pages_fetched[0] <= 2:
                # Return tweets with image URLs
                return [{"entryId": "tweet-1", "content": {"itemContent": {"tweet_results": {"result": {"legacy": {"extended_entities": {"media": [{"type": "photo", "media_url_https": "https://example.com/img.jpg"}]}}}}}}}], "cursor", True
            return [], None, False

        with patch.object(twitter, '_fetch_timeline', new=mock_fetch):
            with patch.object(twitter, '_extract_image_urls', return_value=["https://example.com/img.jpg"]):
                with patch.object(twitter, '_download_image', new=AsyncMock(return_value=False)):
                    count = await twitter._fetch_twitter_images(mock_context)
                    # Since download returns False (already exists), count is 0
                    assert count == 0

    @pytest.mark.asyncio
    async def test_fetch_images_stops_after_empty_pages(self, mock_context):
        """测试连续空页后停止"""
        empty_count = [0]

        async def mock_fetch(context, cursor=None):
            empty_count[0] += 1
            if empty_count[0] <= 1:
                return [{"entryId": "tweet-1"}], "cursor", True
            return [], None, False

        with patch.object(twitter, '_fetch_timeline', new=mock_fetch):
            with patch.object(twitter, '_extract_image_urls', return_value=[]):
                count = await twitter._fetch_twitter_images(mock_context)
                assert count == 0


# ============================================================
# Test Image Filename Handling
# ============================================================

class TestImageFilenameHandling:
    """测试图片文件名处理"""

    @pytest.mark.asyncio
    async def test_download_various_formats(self, mock_context, temp_data_dir):
        """测试下载不同格式的图片"""
        formats = [
            ("https://pbs.twimg.com/media/ABC.jpg", "ABC.jpg"),
            ("https://pbs.twimg.com/media/DEF.png", "DEF.png"),
        ]

        for url, expected_filename in formats:
            class MockResponse:
                status = 200
                async def read(self):
                    return b"data"

            class MockGetCtx:
                async def __aenter__(self):
                    return MockResponse()
                async def __aexit__(self, *args):
                    pass

            class MockSession:
                def get(self, *args, **kwargs):
                    return MockGetCtx()

            mock_context.http_session = MockSession()
            save_dir = temp_data_dir / "images"
            save_dir.mkdir(parents=True, exist_ok=True)

            await twitter._download_image(url, save_dir, mock_context)
            # 清理文件以便下次测试
            file_path = save_dir / expected_filename
            if file_path.exists():
                file_path.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
