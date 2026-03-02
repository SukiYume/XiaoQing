"""
smalltalk 插件单元测试

测试闲聊插件的以下功能:
1. 随机回复 (只叫 bot_name 时)
2. 问答对管理 (添加、查询、删除)
3. 闲聊处理 (handle_smalltalk)
4. 帮助命令
5. 错误处理
"""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import sys

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util

# 动态加载 smalltalk 插件
spec = importlib.util.spec_from_file_location(
    "smalltalk_main",
    ROOT / "plugins" / "smalltalk" / "main.py"
)
smalltalk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smalltalk)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_data_dir():
    """创建临时数据目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        yield data_dir


@pytest.fixture
def mock_context(temp_data_dir):
    """模拟插件上下文"""
    class MockContext:
        def __init__(self, data_dir):
            self.data_dir = data_dir
            self.plugin_dir = ROOT / "plugins" / "smalltalk"
            self.config = {
                "plugins": {
                    "smalltalk": {
                        "voice_probability": 0.0  # 禁用语音转换以简化测试
                    }
                }
            }
            self.secrets = {}
            self.logger = MagicMock()

    return MockContext(temp_data_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": "12345",
        "message": "test",
        "message_type": "private"
    }


@pytest.fixture
def mock_group_event():
    """模拟群聊事件"""
    return {
        "user_id": "12345",
        "group_id": "54321",
        "message": "test",
        "message_type": "group"
    }


# ============================================================
# Test Plugin Initialization
# ============================================================

class TestInit:
    """测试插件初始化"""

    def test_init(self):
        """测试插件初始化"""
        smalltalk.init()
        assert True


# ============================================================
# Test Random Responses (call_bot_name_only)
# ============================================================

class TestRandomResponses:
    """测试随机回复功能"""

    def test_default_responses(self, mock_context):
        """测试默认回复列表"""
        responses = smalltalk._load_responses(mock_context)
        assert responses == smalltalk.DEFAULT_RESPONSES
        assert len(responses) > 0
        assert "叫我干嘛" in responses

    def test_call_bot_name_only(self, mock_context):
        """测试只叫 bot_name 时的回复"""
        result = smalltalk.call_bot_name_only(mock_context)
        assert result is not None
        assert isinstance(result, list)
        assert len(result) > 0
        # 检查返回的是消息段格式
        assert "type" in result[0] or "data" in result[0]

    def test_response_variety(self, mock_context):
        """测试随机回复的多样性"""
        # 多次调用，应该能获取到不同的回复
        responses_set = set()
        for _ in range(50):
            result = smalltalk.call_bot_name_only(mock_context)
            text = result[0].get("data", {}).get("text", "")
            responses_set.add(text)

        # 默认回复有9条，随机50次应该至少获取到几种不同的
        assert len(responses_set) >= 2

    def test_custom_responses_from_file(self, mock_context, temp_data_dir):
        """测试从文件加载自定义回复"""
        # 创建自定义回复文件
        custom_file = temp_data_dir / "responses.json"
        custom_data = {
            "responses": ["自定义回复1", "自定义回复2", "自定义回复3"]
        }
        custom_file.write_text(
            json.dumps(custom_data, ensure_ascii=False),
            encoding="utf-8"
        )

        responses = smalltalk._load_responses(mock_context)
        assert responses == custom_data["responses"]

    def test_custom_responses_from_xiaoqing_file(self, mock_context, temp_data_dir):
        """测试从 小青.json 加载自定义回复"""
        # 创建 小青.json 文件
        xiaoqing_file = temp_data_dir / "小青.json"
        custom_data = {
            "小青": ["小青在", "小青来啦", "小青在此"]
        }
        xiaoqing_file.write_text(
            json.dumps(custom_data, ensure_ascii=False),
            encoding="utf-8"
        )

        responses = smalltalk._load_responses(mock_context)
        assert responses == custom_data["小青"]

    def test_xiaoqing_file_has_priority(self, mock_context, temp_data_dir):
        """测试 小青.json 优先级高于 responses.json"""
        # 创建两个文件
        xiaoqing_file = temp_data_dir / "小青.json"
        xiaoqing_data = {"小青": ["优先级回复"]}
        xiaoqing_file.write_text(
            json.dumps(xiaoqing_data, ensure_ascii=False),
            encoding="utf-8"
        )

        responses_file = temp_data_dir / "responses.json"
        responses_data = {"responses": ["低优先级回复"]}
        responses_file.write_text(
            json.dumps(responses_data, ensure_ascii=False),
            encoding="utf-8"
        )

        responses = smalltalk._load_responses(mock_context)
        assert responses == xiaoqing_data["小青"]


# ============================================================
# Test QA Pairs Management
# ============================================================

class TestQAPairs:
    """测试问答对管理"""

    @pytest.mark.asyncio
    async def test_add_qa_pair(self, mock_context, mock_event):
        """测试添加问答对"""
        result = await smalltalk.handle("qa", "你好 你好呀", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "成功" in result_text or "添加" in result_text

        # 验证保存
        answer = smalltalk.get_qa_answer(mock_context, "你好")
        assert answer is not None
        assert "你好呀" in answer

    @pytest.mark.asyncio
    async def test_add_multiple_answers_same_question(self, mock_context, mock_event):
        """测试同一问题添加多个回答"""
        await smalltalk.handle("qa", "你好 回答1", mock_event, mock_context)
        await smalltalk.handle("qa", "你好 回答2", mock_event, mock_context)
        await smalltalk.handle("qa", "你好 回答3", mock_event, mock_context)

        data = smalltalk._load_qa(mock_context)
        assert "你好" in data
        assert len(data["你好"]) == 3

    @pytest.mark.asyncio
    async def test_add_duplicate_answer(self, mock_context, mock_event):
        """测试添加重复回答"""
        await smalltalk.handle("qa", "测试 相同的回答", mock_event, mock_context)
        result = await smalltalk.handle("qa", "测试 相同的回答", mock_event, mock_context)

        result_text = str(result)
        assert "知道" in result_text or "已经" in result_text

        # 验证不会重复添加
        data = smalltalk._load_qa(mock_context)
        assert len(data["测试"]) == 1

    @pytest.mark.asyncio
    async def test_add_qa_missing_answer(self, mock_context, mock_event):
        """测试缺少回答的情况"""
        result = await smalltalk.handle("qa", "只有问题", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "格式" in result_text or "问题" in result_text

    @pytest.mark.asyncio
    async def test_qa_answer_randomness(self, mock_context, mock_event):
        """测试回答的随机性"""
        await smalltalk.handle("qa", "随机 回答A", mock_event, mock_context)
        await smalltalk.handle("qa", "随机 回答B", mock_event, mock_context)
        await smalltalk.handle("qa", "随机 回答C", mock_event, mock_context)

        answers_set = set()
        for _ in range(30):
            answer = smalltalk.get_qa_answer(mock_context, "随机")
            if answer:
                answers_set.add(answer)

        # 应该至少获取到2种不同的回答
        assert len(answers_set) >= 2

    @pytest.mark.asyncio
    async def test_list_all_qa(self, mock_context, mock_event):
        """测试列出所有问答对"""
        await smalltalk.handle("qa", "问题1 回答1", mock_event, mock_context)
        await smalltalk.handle("qa", "问题2 回答2", mock_event, mock_context)

        result = await smalltalk.handle("qa_list", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "问题1" in result_text
        assert "问题2" in result_text

    @pytest.mark.asyncio
    async def test_list_empty_qa(self, mock_context, mock_event):
        """测试列出空的问答对"""
        result = await smalltalk.handle("qa_list", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "没有" in result_text or "暂无" in result_text or "任何" in result_text

    @pytest.mark.asyncio
    async def test_list_specific_question(self, mock_context, mock_event):
        """测试查询特定问题"""
        await smalltalk.handle("qa", "天气 今天天气很好", mock_event, mock_context)
        await smalltalk.handle("qa", "天气 明天会下雨", mock_event, mock_context)

        result = await smalltalk.handle("qa_list", "天气", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "天气" in result_text
        assert "今天天气很好" in result_text or "明天会下雨" in result_text

    @pytest.mark.asyncio
    async def test_list_nonexistent_question(self, mock_context, mock_event):
        """测试查询不存在的问题"""
        result = await smalltalk.handle("qa_list", "不存在的问题", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "没有" in result_text or "不存在" in result_text

    @pytest.mark.asyncio
    async def test_remove_qa_question(self, mock_context, mock_event):
        """测试删除整个问题"""
        await smalltalk.handle("qa", "待删除 回答1", mock_event, mock_context)
        await smalltalk.handle("qa", "待删除 回答2", mock_event, mock_context)

        result = await smalltalk.handle("qa_remove", "待删除", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "成功" in result_text or "删除" in result_text

        # 验证删除
        data = smalltalk._load_qa(mock_context)
        assert "待删除" not in data

    @pytest.mark.asyncio
    async def test_remove_qa_specific_answer(self, mock_context, mock_event):
        """测试删除特定回答"""
        await smalltalk.handle("qa", "部分删除 回答A", mock_event, mock_context)
        await smalltalk.handle("qa", "部分删除 回答B", mock_event, mock_context)

        result = await smalltalk.handle(
            "qa_remove",
            "部分删除 回答A",
            mock_event,
            mock_context
        )
        assert result is not None

        # 验证只删除了一个回答
        data = smalltalk._load_qa(mock_context)
        assert "部分删除" in data
        assert len(data["部分删除"]) == 1
        assert "回答B" in data["部分删除"]

    @pytest.mark.asyncio
    async def test_remove_nonexistent_question(self, mock_context, mock_event):
        """测试删除不存在的问题"""
        result = await smalltalk.handle(
            "qa_remove",
            "不存在的问题xyz",
            mock_event,
            mock_context
        )
        assert result is not None
        result_text = str(result)
        assert "没有" in result_text or "似乎" in result_text or "不存在" in result_text

    @pytest.mark.asyncio
    async def test_remove_nonexistent_answer(self, mock_context, mock_event):
        """测试删除不存在的回答"""
        await smalltalk.handle("qa", "测试问题 有效回答", mock_event, mock_context)

        result = await smalltalk.handle(
            "qa_remove",
            "测试问题 不存在的回答",
            mock_event,
            mock_context
        )
        assert result is not None
        result_text = str(result)
        assert "没有" in result_text or "不存在" in result_text

    @pytest.mark.asyncio
    async def test_remove_last_answer_removes_question(self, mock_context, mock_event):
        """测试删除最后一个回答时移除问题"""
        await smalltalk.handle("qa", "临时 仅有的回答", mock_event, mock_context)

        # 删除唯一的回答
        await smalltalk.handle("qa_remove", "临时 仅有的回答", mock_event, mock_context)

        # 问题应该被移除
        data = smalltalk._load_qa(mock_context)
        assert "临时" not in data

    @pytest.mark.asyncio
    async def test_qa_persistence(self, mock_context, mock_event, temp_data_dir):
        """测试问答对持久化"""
        await smalltalk.handle("qa", "持久化 测试数据", mock_event, mock_context)

        # 验证文件存在
        qa_file = temp_data_dir / "QA.json"
        assert qa_file.exists()

        # 验证文件内容
        content = qa_file.read_text(encoding="utf-8")
        data = json.loads(content)
        assert "持久化" in data
        assert "测试数据" in data["持久化"]


# ============================================================
# Test Help Commands
# ============================================================

class TestHelp:
    """测试帮助命令"""

    @pytest.mark.asyncio
    async def test_qa_help(self, mock_context, mock_event):
        """测试 qa 帮助"""
        result = await smalltalk.handle("qa", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "问答" in result_text or "记忆" in result_text

    @pytest.mark.asyncio
    async def test_qa_help_chinese(self, mock_context, mock_event):
        """测试 qa 中文帮助"""
        result = await smalltalk.handle("qa", "帮助", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_qa_help_question_mark(self, mock_context, mock_event):
        """测试 qa 问号帮助"""
        result = await smalltalk.handle("qa", "?", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_qa_list_help(self, mock_context, mock_event):
        """测试 qa_list 帮助"""
        result = await smalltalk.handle("qa_list", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "查询" in result_text or "对话" in result_text

    @pytest.mark.asyncio
    async def test_qa_remove_help(self, mock_context, mock_event):
        """测试 qa_remove 帮助"""
        result = await smalltalk.handle("qa_remove", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "删除" in result_text


# ============================================================
# Test Smalltalk Handler
# ============================================================

class TestSmalltalkHandler:
    """测试闲聊处理器"""

    @pytest.mark.asyncio
    async def test_smalltalk_with_qa_match(self, mock_context, mock_event):
        """测试问答匹配的闲聊"""
        # 添加问答对
        await smalltalk.handle("qa", "你好 你好呀！", mock_event, mock_context)

        # 调用闲聊处理
        result = await smalltalk.handle_smalltalk("你好", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0
        result_text = str(result)
        assert "你好呀" in result_text or "你好" in result_text

    @pytest.mark.asyncio
    async def test_smalltalk_no_match_returns_fallback(self, mock_context, mock_event):
        """测试无匹配时返回 fallback"""
        # 模拟 chat 插件调用
        with patch.object(smalltalk, '_call_chat_api', new=AsyncMock()) as mock_chat:
            mock_chat.return_value = smalltalk.segments("测试回复")

            result = await smalltalk.handle_smalltalk("随便说点什么", mock_event, mock_context)
            assert result is not None
            assert len(result) > 0
            mock_chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_smalltalk_chat_plugin_unavailable(self, mock_context, mock_event):
        """测试 chat 插件不可用的情况"""
        import sys
        
        # 使用 patch.dict 将 plugins.chat 设置为 None，强制引发 ImportError
        with patch.dict(sys.modules, {'plugins.chat': None, 'plugins.chat.main': None}):
            result = await smalltalk.handle_smalltalk("测试", mock_event, mock_context)
            assert result is not None
            # 应该返回默认回复
            result_text = str(result)
            assert "无法" in result_text or "暂时" in result_text

    @pytest.mark.asyncio
    async def test_smalltalk_chat_plugin_error(self, mock_context, mock_event):
        """测试 chat 插件调用失败的情况"""
        # 通过模拟 chat_plugin.handle 抛出异常
        import sys
        from unittest.mock import MagicMock

        # 创建一个会抛出异常的 mock chat plugin
        mock_chat_plugin = MagicMock()
        mock_chat_plugin.handle = AsyncMock(side_effect=Exception("API error"))

        # 将 mock 插入到 sys.modules
        sys.modules['plugins.chat'] = MagicMock()
        sys.modules['plugins.chat.main'] = mock_chat_plugin

        try:
            result = await smalltalk.handle_smalltalk("测试", mock_event, mock_context)
            assert result is not None
            # 应该返回错误提示
            result_text = str(result)
            assert "无法" in result_text or "稍后" in result_text
        finally:
            # 清理 mock
            if 'plugins.chat' in sys.modules:
                del sys.modules['plugins.chat']
            if 'plugins.chat.main' in sys.modules:
                del sys.modules['plugins.chat.main']


# ============================================================
# Test Voice Conversion
# ============================================================

class TestVoiceConversion:
    """测试语音转换功能"""

    @pytest.mark.asyncio
    async def test_voice_conversion_disabled(self, mock_context):
        """测试禁用语音转换"""
        # 设置语音概率为 0
        mock_context.config["plugins"]["smalltalk"]["voice_probability"] = 0.0

        reply = smalltalk.segments("测试文本")
        result = await smalltalk._maybe_convert_to_voice(reply, mock_context)

        # 应该返回原始文本
        assert result == reply

    @pytest.mark.asyncio
    async def test_voice_conversion_enabled_no_plugin(self, mock_context):
        """测试启用语音转换但 voice 插件不存在"""
        # 设置语音概率为 1
        mock_context.config["plugins"]["smalltalk"]["voice_probability"] = 1.0

        # 模拟 voice 插件导入失败
        async def mock_import_error():
            raise ImportError("Voice plugin not found")

        reply = smalltalk.segments("测试文本")

        # 模拟导入 voice 插件时出错
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'plugins.voice':
                raise ImportError("Voice plugin not found")
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=mock_import):
            result = await smalltalk._maybe_convert_to_voice(reply, mock_context)

            # 应该返回原始文本（因为插件不可用）
            assert len(result) > 0
            assert result[0].get("type") == "text"

    @pytest.mark.asyncio
    async def test_voice_conversion_empty_text(self, mock_context):
        """测试空文本的语音转换"""
        mock_context.config["plugins"]["smalltalk"]["voice_probability"] = 1.0

        reply = []  # 空列表
        result = await smalltalk._maybe_convert_to_voice(reply, mock_context)

        # 应该返回空列表
        assert result == []

    @pytest.mark.asyncio
    async def test_voice_conversion_with_non_text_segment(self, mock_context):
        """测试包含非文本段的消息"""
        mock_context.config["plugins"]["smalltalk"]["voice_probability"] = 1.0

        reply = [{"type": "image", "data": {"file": "test.jpg"}}]
        result = await smalltalk._maybe_convert_to_voice(reply, mock_context)

        # 应该返回原始消息
        assert result == reply


# ============================================================
# Test Edge Cases
# ============================================================

class TestEdgeCases:
    """测试边界情况"""

    @pytest.mark.asyncio
    async def test_qa_with_special_chars(self, mock_context, mock_event):
        """测试特殊字符的问答对"""
        special_question = "测试@#$%"
        special_answer = "回答!@#$%^&*()"

        result = await smalltalk.handle(
            "qa",
            f"{special_question} {special_answer}",
            mock_event,
            mock_context
        )
        assert result is not None

        # 验证可以检索到
        answer = smalltalk.get_qa_answer(mock_context, special_question)
        assert answer is not None
        assert special_answer in answer

    @pytest.mark.asyncio
    async def test_qa_with_unicode(self, mock_context, mock_event):
        """测试 Unicode 字符"""
        unicode_question = "Hello 世界 🎉"
        unicode_answer = "回答 العربية 🌟"

        result = await smalltalk.handle(
            "qa",
            f"{unicode_question} {unicode_answer}",
            mock_event,
            mock_context
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_qa_with_very_long_content(self, mock_context, mock_event):
        """测试超长内容"""
        long_question = "长问题" * 50
        long_answer = "长回答" * 100

        result = await smalltalk.handle(
            "qa",
            f"{long_question} {long_answer}",
            mock_event,
            mock_context
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_qa_with_newlines(self, mock_context, mock_event):
        """测试包含换行符的内容"""
        question = "多行问题"
        answer = "第一行\n第二行\n第三行"

        result = await smalltalk.handle(
            "qa",
            f"{question} {answer}",
            mock_event,
            mock_context
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_remove_qa_empty_args(self, mock_context, mock_event):
        """测试删除问答对时参数为空"""
        result = await smalltalk.handle("qa_remove", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "删除" in result_text or "格式" in result_text or "哪个" in result_text

    @pytest.mark.asyncio
    async def test_unknown_command(self, mock_context, mock_event):
        """测试未知命令"""
        result = await smalltalk.handle("unknown_command", "args", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "未知" in result_text or "命令" in result_text

    @pytest.mark.asyncio
    async def test_handle_exception_handling(self, mock_context, mock_event):
        """测试处理异常的情况"""
        # 通过传入无效参数触发异常
        with patch.object(smalltalk, 'parse', side_effect=Exception("Test error")):
            result = await smalltalk.handle("qa", "test", mock_event, mock_context)
            assert result is not None
            result_text = str(result)
            assert "错误" in result_text or "出错" in result_text


# ============================================================
# Test Constants
# ============================================================

class TestConstants:
    """测试常量配置"""

    def test_group_random_reply_rate(self):
        """测试群聊随机回复概率"""
        assert smalltalk.GROUP_RANDOM_REPLY_RATE == 0.05

    def test_default_responses_not_empty(self):
        """测试默认回复列表不为空"""
        assert len(smalltalk.DEFAULT_RESPONSES) > 0
        assert all(isinstance(r, str) for r in smalltalk.DEFAULT_RESPONSES)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
