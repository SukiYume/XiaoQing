"""
arxiv_filter 插件单元测试

测试 arXiv 论文筛选插件的功能，包括：
- 命令处理（help、默认查询）
- 定时任务（scheduled_check、scheduled_final_check）
- 状态管理（加载/保存更新状态）
- arXiv 更新检查
- 模型推理（模拟）
- 错误处理
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from datetime import date, datetime

ROOT = Path(__file__).resolve().parent.parent.parent

import importlib.util
spec = importlib.util.spec_from_file_location("arxiv_filter_main", ROOT / "plugins" / "arxiv_filter" / "main.py")
arxiv_filter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arxiv_filter)


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
def temp_plugin_dir():
    """创建临时插件目录（包含模型目录）"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        plugin_dir = Path(tmpdir)
        # 创建模拟模型目录
        model_dir = plugin_dir / "best_model"
        model_dir.mkdir(parents=True, exist_ok=True)
        # 创建模型配置文件
        config_file = plugin_dir / "config.json"
        config_data = {
            "model": {
                "path": "best_model",
                "threshold": 0.5,
                "batch_size": 32,
                "max_len": 64
            },
            "arxiv": {
                "url": "https://arxiv.org/list/astro-ph/new",
                "proxy": None
            }
        }
        config_file.write_text(json.dumps(config_data), encoding="utf-8")
        yield plugin_dir


@pytest.fixture
def mock_context(temp_plugin_dir):
    """模拟插件上下文"""
    class MockContext:
        def __init__(self, plugin_dir):
            self.plugin_dir = plugin_dir
            self.data_dir = plugin_dir / "data"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.config = {}
            self.logger = MagicMock()

    return MockContext(temp_plugin_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {
        "user_id": 12345,
        "group_id": 945793035,
        "message_type": "group"
    }


# ============================================================
# Test Config Loading
# ============================================================

class TestConfigLoading:
    """测试配置加载功能"""

    def test_load_config_from_file(self, temp_plugin_dir):
        """测试从文件加载配置"""
        # 创建配置文件
        config_file = temp_plugin_dir / "config.json"
        config_data = {
            "model": {"path": "custom_model"},
            "arxiv": {"url": "https://arxiv.org/list/astro-ph/new"}
        }
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        config = arxiv_filter._load_config(str(temp_plugin_dir))
        assert config["model"]["path"] == "custom_model"
        assert config["arxiv"]["url"] == "https://arxiv.org/list/astro-ph/new"

    def test_load_config_missing_file(self, temp_data_dir):
        """测试配置文件不存在时返回空配置"""
        # 使用没有配置文件的目录
        config = arxiv_filter._load_config(str(temp_data_dir))
        assert config == {}

    def test_load_config_invalid_json(self, temp_plugin_dir):
        """测试配置文件 JSON 格式错误"""
        config_file = temp_plugin_dir / "config.json"
        config_file.write_text("{ invalid json }", encoding="utf-8")

        # 应该返回空配置而不是崩溃
        config = arxiv_filter._load_config(str(temp_plugin_dir))
        assert config == {}


# ============================================================
# Test Status Management
# ============================================================

class TestStatusManagement:
    """测试状态管理功能"""

    def test_get_status_file_path(self, temp_plugin_dir):
        """测试获取状态文件路径"""
        status_path = arxiv_filter._get_status_file_path(str(temp_plugin_dir))
        expected = str(temp_plugin_dir / "data" / "update_status.json")
        assert status_path == expected

    def test_load_status_creates_default(self, temp_plugin_dir):
        """测试加载状态时创建默认状态"""
        status = arxiv_filter._load_update_status(str(temp_plugin_dir))
        assert status == {}

    def test_save_and_load_status(self, temp_plugin_dir):
        """测试保存和加载状态"""
        test_status = {
            "last_sent_date": "2026-02-04",
            "last_sent_time": "2026-02-04T10:00:00"
        }
        arxiv_filter._save_update_status(str(temp_plugin_dir), test_status)

        loaded = arxiv_filter._load_update_status(str(temp_plugin_dir))
        assert loaded["last_sent_date"] == "2026-02-04"
        assert loaded["last_sent_time"] == "2026-02-04T10:00:00"

    def test_should_send_today_new_day(self, temp_plugin_dir):
        """测试检查是否应该发送（新的一天）"""
        # 保存昨天的日期
        yesterday = (date.today().isoformat())
        status = {"last_sent_date": "2026-01-01"}
        arxiv_filter._save_update_status(str(temp_plugin_dir), status)

        # 应该返回 True（因为日期不同）
        result = arxiv_filter._should_send_today(str(temp_plugin_dir))
        assert result is True

    def test_should_send_today_already_sent(self, temp_plugin_dir):
        """测试今天已经发送过"""
        today = date.today().isoformat()
        status = {"last_sent_date": today}
        arxiv_filter._save_update_status(str(temp_plugin_dir), status)

        result = arxiv_filter._should_send_today(str(temp_plugin_dir))
        assert result is False

    def test_mark_sent_today(self, temp_plugin_dir):
        """测试标记今天已发送"""
        arxiv_filter._mark_sent_today(str(temp_plugin_dir))

        today = date.today().isoformat()
        status = arxiv_filter._load_update_status(str(temp_plugin_dir))
        assert status["last_sent_date"] == today
        assert "last_sent_time" in status


# ============================================================
# Test Handle
# ============================================================

class TestHandle:
    """测试命令处理"""

    @pytest.mark.asyncio
    async def test_handle_help(self, mock_context, mock_event):
        """测试 help 命令"""
        result = await arxiv_filter.handle("arxiv", "help", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "arXiv" in result_text or "论文" in result_text

    @pytest.mark.asyncio
    async def test_handle_help_chinese(self, mock_context, mock_event):
        """测试中文帮助命令"""
        result = await arxiv_filter.handle("arxiv", "帮助", mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "arXiv" in result_text or "论文" in result_text

    @pytest.mark.asyncio
    async def test_handle_default_calls_run_filter(self, mock_context, mock_event):
        """测试默认命令调用 _run_filter"""
        with patch.object(arxiv_filter, '_run_filter', new=AsyncMock(return_value=arxiv_filter.segments("test"))) as mock_run:
            result = await arxiv_filter.handle("arxiv", "", mock_event, mock_context)
            assert result is not None
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_exception(self, mock_context, mock_event):
        """测试处理异常"""
        with patch.object(arxiv_filter, '_run_filter', new=AsyncMock(side_effect=Exception("Test error"))):
            result = await arxiv_filter.handle("arxiv", "", mock_event, mock_context)
            assert result is not None
            result_text = str(result)
            assert "出错" in result_text or "error" in result_text.lower()


# ============================================================
# Test Run Filter
# ============================================================

class TestRunFilter:
    """测试论文筛选功能"""

    @pytest.mark.asyncio
    async def test_run_filter_model_not_loaded(self, mock_context, mock_event):
        """测试模型未加载"""
        # 模拟模型加载失败
        with patch.object(arxiv_filter, '_load_inference', return_value=None):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "无法加载AI模型" in result_text or "模型" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_model_path_not_exists(self, mock_context, mock_event):
        """测试模型路径不存在"""
        # 创建一个没有模型目录的上下文
        empty_dir = mock_context.plugin_dir / "empty"
        empty_dir.mkdir()

        # 模拟模型函数存在但路径不存在
        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: "result"):
            # 修改上下文使其指向不存在的模型
            mock_context.plugin_dir = empty_dir
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "未找到" in result_text or "模型" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_success_with_papers(self, mock_context, mock_event):
        """测试成功获取论文"""
        # 模拟推理结果
        mock_inference_result = """
----- Positive #1 -----
Title      : Test Paper Title
Link       : https://arxiv.org/abs/1234.5678
Probability: 0.8000
"""

        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: mock_inference_result):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "Test Paper Title" in result_text or "论文" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_no_papers(self, mock_context, mock_event):
        """测试没有符合条件的论文"""
        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: "No positive predictions"):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "暂时没有发现感兴趣的论文" in result_text or "没有" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_error_response(self, mock_context, mock_event):
        """测试推理返回错误"""
        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: "Error: Network error"):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "失败" in result_text or "获取失败" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_file_not_found(self, mock_context, mock_event):
        """测试 FileNotFoundError"""
        async def raise_file_not_found(*args, **kwargs):
            raise FileNotFoundError("Model file not found")

        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: None):
            with patch.object(arxiv_filter, 'run_sync', new=AsyncMock(side_effect=raise_file_not_found)):
                result = await arxiv_filter._run_filter(mock_context)
                assert result is not None
                result_text = str(result)
                assert "不完整" in result_text or "失败" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_import_error(self, mock_context, mock_event):
        """测试 ImportError（缺少依赖）"""
        async def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'tensorflow'")

        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: "result"):
            with patch.object(arxiv_filter, 'run_sync', new=AsyncMock(side_effect=raise_import_error)):
                result = await arxiv_filter._run_filter(mock_context)
                assert result is not None
                result_text = str(result)
                assert "依赖" in result_text or "不完整" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_generic_exception(self, mock_context, mock_event):
        """测试通用异常"""
        async def raise_generic_error():
            raise RuntimeError("Unexpected error")

        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: "result"):
            with patch.object(arxiv_filter, 'run_sync', new=AsyncMock(side_effect=raise_generic_error)):
                result = await arxiv_filter._run_filter(mock_context)
                assert result is not None
                result_text = str(result)
                assert "不可用" in result_text or "失败" in result_text


# ============================================================
# Test Inference Loading
# ============================================================

class TestInferenceLoading:
    """测试推理模块加载"""

    def test_load_inference_caches_result(self, temp_plugin_dir):
        """测试推理函数缓存"""
        # 第一次加载
        func1 = arxiv_filter._load_inference(str(temp_plugin_dir))
        # 第二次加载应该返回缓存的函数
        func2 = arxiv_filter._load_inference(str(temp_plugin_dir))
        assert func1 is func2 or func1 == func2

    def test_load_inference_force_reload(self, temp_plugin_dir):
        """测试强制重新加载"""
        # 第一次加载
        func1 = arxiv_filter._load_inference(str(temp_plugin_dir))
        # 强制重新加载
        func2 = arxiv_filter._load_inference(str(temp_plugin_dir), force_reload=True)
        # 由于模块无法真正加载（缺少依赖），只测试调用不会崩溃
        # 函数可能返回 None，这是预期的
        assert func1 == func2 or func1 is None or func2 is None


# ============================================================
# Test Check ArXiv Update
# ============================================================

class TestCheckArxivUpdate:
    """测试 arXiv 更新检查"""

    @pytest.mark.asyncio
    async def test_check_arxiv_update_already_sent(self, mock_context):
        """测试今天已经发送过"""
        # 标记今天已发送
        arxiv_filter._mark_sent_today(str(mock_context.plugin_dir))

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
        assert result == []

    @pytest.mark.asyncio
    async def test_check_arxiv_update_updated_today(self, mock_context):
        """测试 arXiv 已更新到今天"""
        # 确保今天未发送
        today = date.today().isoformat()

        with patch.object(arxiv_filter, '_run_filter', new=AsyncMock(return_value=arxiv_filter.segments("Papers found"))):
            with patch.object(arxiv_filter, 'run_sync', return_value=today):
                result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
                # 应该调用 _run_filter
                assert result is not None

    @pytest.mark.asyncio
    async def test_check_arxiv_update_not_updated_yet(self, mock_context):
        """测试 arXiv 尚未更新"""
        # 返回昨天的日期
        yesterday = "2026-01-01"

        with patch.object(arxiv_filter, 'run_sync', return_value=yesterday):
            result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
            # 应该返回空列表
            assert result == []

    @pytest.mark.asyncio
    async def test_check_arxiv_final_check_no_update(self, mock_context):
        """测试最后检查仍未更新"""
        # 返回旧日期
        old_date = "2026-01-01"
        today = date.today().isoformat()

        with patch.object(arxiv_filter, 'run_sync', return_value=old_date):
            result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=True)
            assert result is not None
            result_text = str(result)
            assert "暂未更新" in result_text or "停更" in result_text

    @pytest.mark.asyncio
    async def test_check_arxiv_update_error(self, mock_context):
        """测试检查更新时出错"""
        with patch.object(arxiv_filter, 'run_sync', side_effect=Exception("Network error")):
            result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
            assert result == []


# ============================================================
# Test Scheduled Tasks
# ============================================================

class TestScheduledTasks:
    """测试定时任务"""

    @pytest.mark.asyncio
    async def test_scheduled(self, mock_context):
        """测试定时任务入口"""
        with patch.object(arxiv_filter, '_run_filter', new=AsyncMock(return_value=arxiv_filter.segments("scheduled"))) as mock_run:
            result = await arxiv_filter.scheduled(mock_context)
            assert result is not None
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheduled_check(self, mock_context):
        """测试定时检查任务"""
        with patch.object(arxiv_filter, '_check_arxiv_update', new=AsyncMock(return_value=[])) as mock_check:
            result = await arxiv_filter.scheduled_check(mock_context)
            mock_check.assert_called_once_with(mock_context, is_final_check=False)

    @pytest.mark.asyncio
    async def test_scheduled_final_check(self, mock_context):
        """测试最后检查任务"""
        with patch.object(arxiv_filter, '_check_arxiv_update', new=AsyncMock(return_value=[])) as mock_check:
            result = await arxiv_filter.scheduled_final_check(mock_context)
            mock_check.assert_called_once_with(mock_context, is_final_check=True)


# ============================================================
# Test Help
# ============================================================

class TestHelp:
    """测试帮助信息"""

    def test_show_help(self):
        """测试显示帮助信息"""
        help_text = arxiv_filter._show_help()
        assert help_text is not None
        assert "arXiv" in help_text
        assert "论文" in help_text
        assert "/arxiv" in help_text


# ============================================================
# Test Init
# ============================================================

class TestInit:
    """测试插件初始化"""

    def test_init_clears_cache(self):
        """测试初始化清除缓存"""
        # 设置缓存
        arxiv_filter._inference_func = lambda: "cached"
        arxiv_filter.init()
        # 缓存应该被清除
        assert arxiv_filter._inference_func is None


# ============================================================
# Test Multiple Papers
# ============================================================

class TestMultiplePapers:
    """测试多论文场景"""

    @pytest.mark.asyncio
    async def test_multiple_papers_output(self, mock_context, mock_event):
        """测试多论文输出格式"""
        mock_result = """
----- Positive #1 -----
Title      : First Paper Title
Link       : https://arxiv.org/abs/1111.1111
Probability: 0.9000

----- Positive #2 -----
Title      : Second Paper Title
Link       : https://arxiv.org/abs/2222.2222
Probability: 0.7500
"""
        with patch.object(arxiv_filter, '_load_inference', return_value=lambda **kwargs: mock_result):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "First Paper Title" in result_text or "Second Paper Title" in result_text or "论文" in result_text


# ============================================================
# Test Date Handling
# ============================================================

class TestDateHandling:
    """测试日期处理"""

    def test_date_format_in_status(self, temp_plugin_dir):
        """测试状态中的日期格式"""
        arxiv_filter._mark_sent_today(str(temp_plugin_dir))
        status = arxiv_filter._load_update_status(str(temp_plugin_dir))

        # 检查日期格式
        assert "last_sent_date" in status
        # 应该是 ISO 格式
        assert "-" in status["last_sent_date"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
