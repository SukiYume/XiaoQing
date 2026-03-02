"""
guess_number 插件单元测试
"""
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("guess_number_main", ROOT / "plugins" / "guess_number" / "main.py")
guess_number = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guess_number)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_context():
    """模拟插件上下文"""
    class MockSession:
        def __init__(self, data=None):
            self._data = data or {}

        def get(self, key, default=None):
            return self._data.get(key, default)

        def set(self, key, value):
            self._data[key] = value

    class MockContext:
        def __init__(self):
            self.plugin_dir = ROOT / "plugins" / "guess_number"
            self.data_dir = self.plugin_dir / "data"
            self._session = None
            self.current_user_id = "test_user_123"

        async def create_session(self, initial_data=None, timeout=180.0):
            self._session = MockSession(initial_data or {})
            return self._session

        async def get_session(self):
            return self._session

        async def end_session(self):
            self._session = None

    return MockContext()


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": "12345",
        "message": "test",
        "message_type": "private"
    }


# ============================================================
# Test Game Start
# ============================================================

class TestGameStart:
    """测试游戏开始功能"""

    @pytest.mark.asyncio
    async def test_start_default_game(self, mock_context, mock_event):
        """测试默认难度开始游戏"""
        result = await guess_number.handle("guess_number", "", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

        # 检查会话已创建
        session = await mock_context.get_session()
        assert session is not None
        assert session.get("target") is not None
        assert session.get("min") == 1
        assert session.get("max") == 100
        assert session.get("max_attempts") == 7

    @pytest.mark.asyncio
    async def test_start_easy_difficulty(self, mock_context, mock_event):
        """测试简单难度"""
        result = await guess_number.handle("guess_number", "easy", mock_event, mock_context)
        assert result is not None

        session = await mock_context.get_session()
        assert session.get("min") == 1
        assert session.get("max") == 50
        assert session.get("max_attempts") == 10

    @pytest.mark.asyncio
    async def test_start_hard_difficulty(self, mock_context, mock_event):
        """测试困难难度"""
        result = await guess_number.handle("guess_number", "hard", mock_event, mock_context)
        assert result is not None

        session = await mock_context.get_session()
        assert session.get("min") == 1
        assert session.get("max") == 200
        assert session.get("max_attempts") == 8

    @pytest.mark.asyncio
    async def test_start_hell_difficulty(self, mock_context, mock_event):
        """测试地狱难度"""
        result = await guess_number.handle("guess_number", "hell", mock_event, mock_context)
        assert result is not None

        session = await mock_context.get_session()
        assert session.get("min") == 1
        assert session.get("max") == 1000
        assert session.get("max_attempts") == 10

    @pytest.mark.asyncio
    async def test_start_chinese_difficulty(self, mock_context, mock_event):
        """测试中文难度参数"""
        result = await guess_number.handle("guess_number", "简单", mock_event, mock_context)
        assert result is not None

        session = await mock_context.get_session()
        assert session.get("max") == 50

    @pytest.mark.asyncio
    async def test_existing_game_check(self, mock_context, mock_event):
        """测试已有游戏时提示"""
        # 先创建一个游戏
        await mock_context.create_session({
            "target": 42,
            "min": 1,
            "max": 100,
            "max_attempts": 7,
            "remaining": 5,
            "hint": "1-100"
        })

        # 尝试再开始一个
        result = await guess_number.handle("guess_number", "", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "进行中" in result_text or "already" in result_text.lower() or "chance" in result_text.lower()


# ============================================================
# Test Guessing
# ============================================================

class TestGuessing:
    """测试猜测功能"""

    @pytest.mark.asyncio
    async def test_correct_guess(self, mock_context, mock_event):
        """测试猜对数字"""
        # 创建目标为 42 的游戏
        session = await mock_context.create_session({
            "target": 42,
            "min": 1,
            "max": 100,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "1-100",
            "history": []
        })

        result = await guess_number.handle_session("42", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "恭喜" in result_text or "成功" in result_text or "猜对" in result_text

        # 会话应该结束
        session_after = await mock_context.get_session()
        assert session_after is None

    @pytest.mark.asyncio
    async def test_wrong_guess_low(self, mock_context, mock_event):
        """测试猜错（太小）"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 100,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "1-100",
            "history": []
        })

        result = await guess_number.handle_session("25", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "小" in result_text or "low" in result_text.lower()

        # 检查下限被更新
        assert session.get("min") == 26

    @pytest.mark.asyncio
    async def test_wrong_guess_high(self, mock_context, mock_event):
        """测试猜错（太大）"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 100,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "1-100",
            "history": []
        })

        result = await guess_number.handle_session("75", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "大" in result_text or "high" in result_text.lower()

        # 检查上限被更新
        assert session.get("max") == 74

    @pytest.mark.asyncio
    async def test_attempts_decrement(self, mock_context, mock_event):
        """测试次数递减"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 100,
            "attempts": 2,
            "max_attempts": 7,
            "remaining": 5,
            "hint": "1-100",
            "history": []
        })

        await guess_number.handle_session("25", mock_event, mock_context, session)

        assert session.get("attempts") == 3
        assert session.get("remaining") == 4

    @pytest.mark.asyncio
    async def test_history_tracking(self, mock_context, mock_event):
        """测试猜测历史记录"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 100,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "1-100",
            "history": []
        })

        await guess_number.handle_session("25", mock_event, mock_context, session)
        await guess_number.handle_session("75", mock_event, mock_context, session)

        history = session.get("history")
        assert history == [25, 75]


# ============================================================
# Test Game End
# ============================================================

class TestGameEnd:
    """测试游戏结束"""

    @pytest.mark.asyncio
    async def test_out_of_attempts(self, mock_context, mock_event):
        """测试次数用尽"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 100,
            "attempts": 6,
            "max_attempts": 7,
            "remaining": 1,
            "hint": "1-100",
            "history": [1, 2, 3, 4, 5, 6]
        })

        result = await guess_number.handle_session("7", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "结束" in result_text or "用尽" in result_text or "次数" in result_text

        # 会话应该结束
        session_after = await mock_context.get_session()
        assert session_after is None


# ============================================================
# Test Status
# ============================================================

class TestGameStatus:
    """测试游戏状态查询"""

    @pytest.mark.asyncio
    async def test_status_with_active_game(self, mock_context, mock_event):
        """测试查询活跃游戏状态"""
        await mock_context.create_session({
            "target": 42,
            "min": 10,
            "max": 50,
            "attempts": 3,
            "max_attempts": 7,
            "remaining": 4,
            "hint": "10-50",
            "history": [20, 30, 40]
        })

        result = await guess_number.handle("guess_number", "status", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "10-50" in result_text or "状态" in result_text

    @pytest.mark.asyncio
    async def test_status_without_game(self, mock_context, mock_event):
        """测试无游戏时查询状态"""
        result = await guess_number.handle("guess_number", "status", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "没有" in result_text or "no" in result_text.lower() or "当前没有" in result_text

    @pytest.mark.asyncio
    async def test_session_status_command(self, mock_context, mock_event):
        """测试会话中的 status 命令"""
        session = await mock_context.create_session({
            "target": 42,
            "min": 1,
            "max": 100,
            "attempts": 2,
            "max_attempts": 7,
            "remaining": 5,
            "hint": "1-50",
            "history": [25, 50]
        })

        result = await guess_number.handle_session("status", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "1-50" in result_text


# ============================================================
# Test Input Validation
# ============================================================

class TestInputValidation:
    """测试输入验证"""

    @pytest.mark.asyncio
    async def test_non_numeric_input(self, mock_context, mock_event):
        """测试非数字输入"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 100,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "1-100",
            "history": []
        })

        result = await guess_number.handle_session("abc", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "数字" in result_text or "number" in result_text.lower()

    @pytest.mark.asyncio
    async def test_out_of_range_low(self, mock_context, mock_event):
        """测试超出下限"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 10,
            "max": 100,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "10-100",
            "history": []
        })

        result = await guess_number.handle_session("5", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "10" in result_text and "100" in result_text

    @pytest.mark.asyncio
    async def test_out_of_range_high(self, mock_context, mock_event):
        """测试超出上限"""
        session = await mock_context.create_session({
            "target": 50,
            "min": 1,
            "max": 50,
            "attempts": 0,
            "max_attempts": 7,
            "remaining": 7,
            "hint": "1-50",
            "history": []
        })

        result = await guess_number.handle_session("100", mock_event, mock_context, session)
        assert result is not None
        result_text = str(result)
        assert "1" in result_text and "50" in result_text


# ============================================================
# Test Restart
# ============================================================

class TestGameRestart:
    """测试游戏重启"""

    @pytest.mark.asyncio
    async def test_restart_command(self, mock_context, mock_event):
        """测试 restart 命令"""
        # 先创建一个游戏
        await mock_context.create_session({
            "target": 42,
            "min": 1,
            "max": 100,
            "attempts": 3,
            "max_attempts": 7,
            "remaining": 4,
            "hint": "1-100",
            "history": []
        })

        result = await guess_number.handle("guess_number", "restart", mock_event, mock_context)
        assert result is not None

        # 应该有新的会话（旧会话被结束）
        session = await mock_context.get_session()
        assert session is not None


# ============================================================
# Test Help
# ============================================================

class TestHelp:
    """测试帮助功能"""

    @pytest.mark.asyncio
    async def test_help_command(self, mock_context, mock_event):
        """测试 help 命令"""
        result = await guess_number.handle("guess_number", "help", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_question_mark(self, mock_context, mock_event):
        """测试 ? 命令"""
        result = await guess_number.handle("guess_number", "?", mock_event, mock_context)
        assert result is not None
        assert len(result) > 0


# ============================================================
# Test Helper Functions
# ============================================================

class TestHelperFunctions:
    """测试辅助函数"""

    def test_parse_difficulty_default(self):
        """测试默认难度解析"""
        result = guess_number._parse_difficulty("")
        assert result == (1, 100, 7)

        result = guess_number._parse_difficulty("invalid")
        assert result == (1, 100, 7)

    def test_parse_difficulty_easy(self):
        """测试简单难度解析"""
        result = guess_number._parse_difficulty("easy")
        assert result == (1, 50, 10)

        result = guess_number._parse_difficulty("e")
        assert result == (1, 50, 10)

        result = guess_number._parse_difficulty("简单")
        assert result == (1, 50, 10)

    def test_parse_difficulty_hard(self):
        """测试困难难度解析"""
        result = guess_number._parse_difficulty("hard")
        assert result == (1, 200, 8)

        result = guess_number._parse_difficulty("h")
        assert result == (1, 200, 8)

        result = guess_number._parse_difficulty("困难")
        assert result == (1, 200, 8)

    def test_parse_difficulty_hell(self):
        """测试地狱难度解析"""
        result = guess_number._parse_difficulty("hell")
        assert result == (1, 1000, 10)

        result = guess_number._parse_difficulty("nightmare")
        assert result == (1, 1000, 10)

        result = guess_number._parse_difficulty("地狱")
        assert result == (1, 1000, 10)

    def test_get_difficulty_name(self):
        """测试难度名称获取"""
        assert "简单" in guess_number._get_difficulty_name("easy")
        assert "普通" in guess_number._get_difficulty_name("")
        assert "困难" in guess_number._get_difficulty_name("hard")
        assert "地狱" in guess_number._get_difficulty_name("hell")

    def test_get_rating(self):
        """测试评价函数"""
        assert "一发入魂" in guess_number._get_rating(1, 7)
        assert "太厉害了" in guess_number._get_rating(2, 7)
        assert "表现优秀" in guess_number._get_rating(3, 7)
        assert "不错" in guess_number._get_rating(4, 7)
        assert "还可以更好" in guess_number._get_rating(6, 7)
        assert "险胜" in guess_number._get_rating(7, 7)

    def test_init(self):
        """测试插件初始化"""
        guess_number.init()
        assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
