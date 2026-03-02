"""测试voice插件 - 语音功能插件"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
from typing import Any, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("voice_main", ROOT / "plugins" / "voice" / "main.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class TestVoicePlugin:
    """测试voice插件"""

    @pytest.fixture
    def mock_context(self, tmp_path):
        """模拟插件上下文"""
        context = MagicMock()
        context.secrets = {
            "plugins": {
                "voice": {
                    "subscription_key": "test_key_123",
                    "region": "southeastasia",
                    "voice_name": "zh-CN-XiaomoNeural",
                    "style": "cheerful",
                    "role": "Girl"
                }
            }
        }
        context.logger = MagicMock()
        context.data_dir = tmp_path / "data"
        context.data_dir.mkdir(parents=True, exist_ok=True)

        # 创建成功的HTTP响应mock
        class MockTTSResponse:
            status = 200
            async def read(self):
                return b"fake_audio_data"

        class MockTTSContextManager:
            async def __aenter__(self):
                return MockTTSResponse()
            async def __aexit__(self, *args):
                pass

        class MockHttpSession:
            def post(self, *args, **kwargs):
                return MockTTSContextManager()

        context.http_session = MockHttpSession()

        return context

    @pytest.fixture
    def mock_event(self):
        """模拟事件"""
        return {
            "user_id": "12345",
            "message": "test",
            "message_type": "private"
        }

    def test_init(self):
        """测试插件初始化"""
        voice.init()
        assert True

    def test_show_help(self):
        """测试帮助信息"""
        help_text = voice._show_help()
        assert help_text is not None
        assert "语音" in help_text or "TTS" in help_text
        assert "/语音" in help_text or "/tts" in help_text

    @pytest.mark.asyncio
    async def test_text_to_speech_success(self, mock_context):
        """测试成功的TTS转换"""
        result = await voice.text_to_speech("你好", mock_context)
        assert result is not None
        assert result.endswith(".mp3")

    @pytest.mark.asyncio
    async def test_text_to_speech_no_subscription_key(self, tmp_path):
        """测试缺少subscription key"""
        context = MagicMock()
        context.secrets = {"plugins": {"voice": {}}}
        context.logger = MagicMock()
        context.data_dir = tmp_path / "data"
        context.data_dir.mkdir(parents=True, exist_ok=True)

        result = await voice.text_to_speech("你好", context)
        assert result is None

    @pytest.mark.asyncio
    async def test_text_to_speech_cached(self, mock_context):
        """测试TTS缓存"""
        # 第一次调用创建缓存文件
        result1 = await voice.text_to_speech("测试", mock_context)
        assert result1 is not None

        # 第二次调用应该使用缓存
        result2 = await voice.text_to_speech("测试", mock_context)
        assert result2 is not None
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_text_to_speech_api_error(self, mock_context):
        """测试TTS API错误"""
        class MockErrorResponse:
            status = 401
            async def read(self):
                return b"error"

        class MockErrorContextManager:
            async def __aenter__(self):
                return MockErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockErrorSession:
            def post(self, *args, **kwargs):
                return MockErrorContextManager()

        mock_context.http_session = MockErrorSession()

        result = await voice.text_to_speech("你好", mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_text_to_speech_exception(self, mock_context):
        """测试TTS异常"""
        class MockExceptionContextManager:
            async def __aenter__(self):
                raise Exception("Network error")
            async def __aexit__(self, *args):
                pass

        class MockExceptionSession:
            def post(self, *args, **kwargs):
                return MockExceptionContextManager()

        mock_context.http_session = MockExceptionSession()

        result = await voice.text_to_speech("你好", mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_speech_to_text_success(self, mock_context, tmp_path):
        """测试成功的STT转换"""
        # 创建临时音频文件
        audio_file = tmp_path / "test_audio.wav"
        audio_file.write_bytes(b"fake_wav_data")

        # 模拟STT响应
        class MockSTTResponse:
            status = 200
            async def json(self):
                return {
                    "NBest": [
                        {
                            "Lexical": "识别结果",
                            "Display": "显示结果"
                        }
                    ]
                }

        class MockSTTContextManager:
            async def __aenter__(self):
                return MockSTTResponse()
            async def __aexit__(self, *args):
                pass

        class MockSTTSession:
            def post(self, *args, **kwargs):
                return MockSTTContextManager()

        mock_context.http_session = MockSTTSession()

        result = await voice.speech_to_text(str(audio_file), mock_context)
        assert result is not None
        assert isinstance(result, tuple)
        assert result[0] == "识别结果"
        assert result[1] == "显示结果"

    @pytest.mark.asyncio
    async def test_speech_to_text_no_subscription_key(self, tmp_path):
        """测试STT缺少subscription key"""
        context = MagicMock()
        context.secrets = {"plugins": {"voice": {}}}
        context.logger = MagicMock()

        audio_file = tmp_path / "test_audio.wav"
        audio_file.write_bytes(b"fake_wav_data")

        result = await voice.speech_to_text(str(audio_file), context)
        assert result is None

    @pytest.mark.asyncio
    async def test_speech_to_text_api_error(self, mock_context, tmp_path):
        """测试STT API错误"""
        audio_file = tmp_path / "test_audio.wav"
        audio_file.write_bytes(b"fake_wav_data")

        class MockSTTErrorResponse:
            status = 401
            async def read(self):
                return b"error"

        class MockSTTErrorContextManager:
            async def __aenter__(self):
                return MockSTTErrorResponse()
            async def __aexit__(self, *args):
                pass

        class MockSTTErrorSession:
            def post(self, *args, **kwargs):
                return MockSTTErrorContextManager()

        mock_context.http_session = MockSTTErrorSession()

        result = await voice.speech_to_text(str(audio_file), mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_speech_to_text_no_result(self, mock_context, tmp_path):
        """测试STT无识别结果"""
        audio_file = tmp_path / "test_audio.wav"
        audio_file.write_bytes(b"fake_wav_data")

        class MockSTTNoResultResponse:
            status = 200
            async def json(self):
                return {"NBest": []}

        class MockSTTNoResultContextManager:
            async def __aenter__(self):
                return MockSTTNoResultResponse()
            async def __aexit__(self, *args):
                pass

        class MockSTTNoResultSession:
            def post(self, *args, **kwargs):
                return MockSTTNoResultContextManager()

        mock_context.http_session = MockSTTNoResultSession()

        result = await voice.speech_to_text(str(audio_file), mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_speech_to_text_exception(self, mock_context, tmp_path):
        """测试STT异常"""
        audio_file = tmp_path / "test_audio.wav"
        audio_file.write_bytes(b"fake_wav_data")

        class MockSTTExceptionContextManager:
            async def __aenter__(self):
                raise Exception("Network error")
            async def __aexit__(self, *args):
                pass

        class MockSTTExceptionSession:
            def post(self, *args, **kwargs):
                return MockSTTExceptionContextManager()

        mock_context.http_session = MockSTTExceptionSession()

        result = await voice.speech_to_text(str(audio_file), mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_handle_tts_help(self, mock_context, mock_event):
        """测试TTS帮助命令"""
        result = await voice.handle("tts", "help", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "语音" in result_text or "帮助" in result_text

    @pytest.mark.asyncio
    async def test_handle_tts_empty_text(self, mock_context, mock_event):
        """测试TTS空文本"""
        result = await voice.handle("tts", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "请输入" in result_text or "转换" in result_text

    @pytest.mark.asyncio
    async def test_handle_tts_success(self, mock_context, mock_event):
        """测试成功的TTS处理"""
        result = await voice.handle("tts", "你好世界", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        # 返回的应该是语音消息段
        assert result[0].get("type") == "record"

    @pytest.mark.asyncio
    async def test_handle_tts_failure(self, mock_context, mock_event):
        """测试TTS失败"""
        class MockFailureResponse:
            status = 500
            async def read(self):
                return b"error"

        class MockFailureContextManager:
            async def __aenter__(self):
                return MockFailureResponse()
            async def __aexit__(self, *args):
                pass

        class MockFailureSession:
            def post(self, *args, **kwargs):
                return MockFailureContextManager()

        mock_context.http_session = MockFailureSession()

        result = await voice.handle("tts", "你好", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "失败" in result_text

    @pytest.mark.asyncio
    async def test_handle_unknown_command(self, mock_context, mock_event):
        """测试未知命令"""
        result = await voice.handle("unknown", "test", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "未知" in result_text

    @pytest.mark.asyncio
    async def test_convert_text_to_voice_success(self, mock_context):
        """测试文字转语音工具函数"""
        result = await voice.convert_text_to_voice("测试", mock_context)
        assert result is not None
        assert isinstance(result, list)
        assert result[0].get("type") == "record"

    @pytest.mark.asyncio
    async def test_convert_text_to_voice_failure(self, mock_context):
        """测试文字转语音工具函数失败"""
        class MockConvertExceptionContextManager:
            async def __aenter__(self):
                raise Exception("Error")
            async def __aexit__(self, *args):
                pass

        class MockConvertExceptionSession:
            def post(self, *args, **kwargs):
                return MockConvertExceptionContextManager()

        mock_context.http_session = MockConvertExceptionSession()

        result = await voice.convert_text_to_voice("测试", mock_context)
        assert result is None

    @pytest.mark.asyncio
    async def test_text_to_speech_creates_audio_dir(self, tmp_path):
        """测试TTS创建音频目录"""
        context = MagicMock()
        context.secrets = {
            "plugins": {
                "voice": {
                    "subscription_key": "test_key"
                }
            }
        }
        context.logger = MagicMock()
        context.data_dir = tmp_path / "data"
        # 不预先创建audio目录

        class MockCreateDirResponse:
            status = 200
            async def read(self):
                return b"audio"

        class MockCreateDirContextManager:
            async def __aenter__(self):
                return MockCreateDirResponse()
            async def __aexit__(self, *args):
                pass

        class MockCreateDirSession:
            def post(self, *args, **kwargs):
                return MockCreateDirContextManager()

        context.http_session = MockCreateDirSession()

        result = await voice.text_to_speech("test", context)
        assert result is not None
        # 验证音频目录已创建
        audio_dir = context.data_dir / "audio"
        assert audio_dir.exists()

    def test_command_triggers(self):
        """测试支持的命令触发词"""
        # plugin.json中定义的触发词: 语音, 念, tts
        assert hasattr(voice, 'handle')
        assert callable(voice.handle)
