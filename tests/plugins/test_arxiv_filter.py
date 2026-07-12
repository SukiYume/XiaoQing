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

import asyncio
import importlib
import json
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from core.interfaces import DeliveryTarget, PluginCapabilities, PluginPrincipal

ROOT = Path(__file__).resolve().parent.parent.parent

arxiv_filter = importlib.import_module("plugins.arxiv_filter.main")
arxiv_codex_summary = importlib.import_module("plugins.arxiv_filter.codex_summary")
arxiv_filter_utils = importlib.import_module("plugins.arxiv_filter.utils")


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
            "model": {"path": "best_model", "threshold": 0.5, "batch_size": 32, "max_len": 64},
            "arxiv": {"url": "https://arxiv.org/list/astro-ph/new", "proxy": None},
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
            self.principal = PluginPrincipal(kind="lifecycle")
            self.capabilities = PluginCapabilities()

    return MockContext(temp_plugin_dir)


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {"user_id": 12345, "group_id": 100000001, "message_type": "group"}


# ============================================================
# Test Config Loading
# ============================================================


class TestConfigLoading:
    """测试配置加载功能"""

    def test_load_config_from_file(self, temp_plugin_dir):
        """测试从文件加载配置（utils.load_plugin_config）"""
        # 创建配置文件
        config_file = temp_plugin_dir / "config.json"
        config_data = {
            "model": {"path": "custom_model"},
            "arxiv": {"url": "https://arxiv.org/list/astro-ph/new"},
        }
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        with patch.object(arxiv_filter_utils, "__file__", str(temp_plugin_dir / "utils.py")):
            config = arxiv_filter_utils.load_plugin_config()
        assert config["model"]["path"] == "custom_model"
        assert config["arxiv"]["url"] == "https://arxiv.org/list/astro-ph/new"

    def test_load_config_missing_file(self, temp_data_dir):
        """测试配置文件不存在时返回空配置（utils.load_plugin_config）"""
        with patch.object(arxiv_filter_utils, "__file__", str(temp_data_dir / "utils.py")):
            config = arxiv_filter_utils.load_plugin_config()
        assert config == {}

    def test_load_config_invalid_json(self, temp_plugin_dir):
        """测试配置文件 JSON 格式错误时抛出异常"""
        config_file = temp_plugin_dir / "config.json"
        config_file.write_text("{ invalid json }", encoding="utf-8")

        with patch.object(arxiv_filter_utils, "__file__", str(temp_plugin_dir / "utils.py")):
            with pytest.raises(json.JSONDecodeError):
                arxiv_filter_utils.load_plugin_config()


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
        test_status = {"last_sent_date": "2026-02-04", "last_sent_time": "2026-02-04T10:00:00"}
        arxiv_filter._save_update_status(str(temp_plugin_dir), test_status)

        loaded = arxiv_filter._load_update_status(str(temp_plugin_dir))
        assert loaded["last_sent_date"] == "2026-02-04"
        assert loaded["last_sent_time"] == "2026-02-04T10:00:00"

    def test_should_send_today_new_day(self, temp_plugin_dir):
        """测试检查是否应该发送（新的一天）"""
        status = {"last_sent_date": "2026-01-01"}
        arxiv_filter._save_update_status(str(temp_plugin_dir), status)

        # 应该返回 True（因为日期不同）
        result = arxiv_filter._should_send_today(str(temp_plugin_dir))
        assert result is True

    def test_should_send_today_already_sent(self, temp_plugin_dir):
        """测试今天已经发送过"""
        today = arxiv_filter._business_now().date().isoformat()
        status = {"last_sent_date": today}
        arxiv_filter._save_update_status(str(temp_plugin_dir), status)

        result = arxiv_filter._should_send_today(str(temp_plugin_dir))
        assert result is False

    def test_mark_sent_today(self, temp_plugin_dir):
        """测试标记今天已发送"""
        arxiv_filter._mark_sent_today(str(temp_plugin_dir))

        today = arxiv_filter._business_now().date().isoformat()
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
        with patch.object(
            arxiv_filter, "_run_filter", new=AsyncMock(return_value=arxiv_filter.segments("test"))
        ) as mock_run:
            result = await arxiv_filter.handle("arxiv", "", mock_event, mock_context)
            assert result is not None
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_enables_codex_sidecar_only_for_current_bot_admin(
        self,
        mock_context,
        mock_event,
    ):
        mock_context.principal = PluginPrincipal(kind="user", user_id=12345, group_id=100000001)
        mock_context.capabilities = PluginCapabilities(is_bot_admin=True)
        with patch.object(
            arxiv_filter,
            "_run_filter",
            new=AsyncMock(return_value=arxiv_filter.segments("test")),
        ) as mock_run:
            await arxiv_filter.handle("arxiv", "", mock_event, mock_context)

        assert mock_run.await_args.kwargs == {
            "allow_codex_sidecar": True,
        }

        mock_event["user_id"] = 99999
        with patch.object(
            arxiv_filter,
            "_run_filter",
            new=AsyncMock(return_value=arxiv_filter.segments("test")),
        ) as mock_run:
            await arxiv_filter.handle("arxiv", "", mock_event, mock_context)

        assert mock_run.await_args.kwargs["allow_codex_sidecar"] is False

    @pytest.mark.asyncio
    async def test_handle_exception(self, mock_context, mock_event):
        """测试处理异常"""
        with patch.object(
            arxiv_filter, "_run_filter", new=AsyncMock(side_effect=Exception("Test error"))
        ):
            result = await arxiv_filter.handle("arxiv", "", mock_event, mock_context)
            assert result is not None
            result_text = str(result)
            assert "XQ-PLUGIN-UNEXPECTED" in result_text


# ============================================================
# Test Run Filter
# ============================================================


class TestRunFilter:
    """测试论文筛选功能"""

    @pytest.mark.asyncio
    async def test_run_filter_model_not_loaded(self, mock_context, mock_event):
        """测试模型未加载"""
        # 模拟模型加载失败
        with patch.object(arxiv_filter, "_load_inference", return_value=None):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "无法加载AI模型" in result_text or "模型" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_model_path_not_exists(self, mock_context, mock_event):
        """测试模型路径不存在时，推理仍可通过（推理函数自行解析路径）"""
        # 创建一个没有模型目录的上下文
        empty_dir = mock_context.plugin_dir / "empty"
        empty_dir.mkdir()

        # 模拟模型函数存在但路径不存在
        # 注: _run_filter 通过 load_plugin_config() 加载配置（基于 __file__），
        #     不直接依赖 context.plugin_dir 来解析模型路径。
        #     因此当推理函数正常返回时，结果会被正常格式化。
        with patch.object(arxiv_filter, "_load_inference", return_value=lambda **kwargs: "result"):
            mock_context.plugin_dir = empty_dir
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            # 推理函数成功返回 "result"，应被正常格式化输出
            assert "论文" in result_text or "result" in result_text

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

        with patch.object(
            arxiv_filter, "_load_inference", return_value=lambda **kwargs: mock_inference_result
        ):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "Test Paper Title" in result_text or "论文" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_no_papers(self, mock_context, mock_event):
        """测试没有符合条件的论文"""
        with patch.object(
            arxiv_filter, "_load_inference", return_value=lambda **kwargs: "No positive predictions"
        ):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "暂时没有发现感兴趣的论文" in result_text or "没有" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_error_response(self, mock_context, mock_event):
        """测试推理返回错误"""
        with patch.object(
            arxiv_filter, "_load_inference", return_value=lambda **kwargs: "Error: Network error"
        ):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert "XQ-PLUGIN-UNEXPECTED" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_file_not_found(self, mock_context, mock_event):
        """测试 FileNotFoundError"""

        async def raise_file_not_found(*args, **kwargs):
            raise FileNotFoundError("Model file not found")

        with patch.object(arxiv_filter, "_load_inference", return_value=lambda **kwargs: None):
            with patch.object(
                arxiv_filter, "run_sync", new=AsyncMock(side_effect=raise_file_not_found)
            ):
                result = await arxiv_filter._run_filter(mock_context)
                assert result is not None
                result_text = str(result)
                assert "XQ-PLUGIN-UNEXPECTED" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_import_error(self, mock_context, mock_event):
        """测试 ImportError（缺少依赖）"""

        async def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'tensorflow'")

        with patch.object(arxiv_filter, "_load_inference", return_value=lambda **kwargs: "result"):
            with patch.object(
                arxiv_filter, "run_sync", new=AsyncMock(side_effect=raise_import_error)
            ):
                result = await arxiv_filter._run_filter(mock_context)
                assert result is not None
                result_text = str(result)
                assert "XQ-PLUGIN-UNEXPECTED" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_generic_exception(self, mock_context, mock_event):
        """测试通用异常"""

        async def raise_generic_error():
            raise RuntimeError("Unexpected error")

        with patch.object(arxiv_filter, "_load_inference", return_value=lambda **kwargs: "result"):
            with patch.object(
                arxiv_filter, "run_sync", new=AsyncMock(side_effect=raise_generic_error)
            ):
                result = await arxiv_filter._run_filter(mock_context)
                assert result is not None
                result_text = str(result)
                assert "XQ-PLUGIN-UNEXPECTED" in result_text


# ============================================================
# Test Inference Loading
# ============================================================


class TestInferenceLoading:
    """测试推理模块加载"""

    def test_load_inference_caches_result(self, temp_plugin_dir):
        """测试推理函数缓存"""
        fake_module = types.ModuleType("plugins.arxiv_filter.arxiv_inference")
        fake_func = lambda **kwargs: "ok"  # noqa: E731
        fake_module.get_positive_arxiv_today_as_string = fake_func

        arxiv_filter._inference_func = None
        with patch.dict(sys.modules, {"plugins.arxiv_filter.arxiv_inference": fake_module}):
            # 第一次加载
            func1 = arxiv_filter._load_inference()
            # 第二次加载应该返回缓存的函数
            func2 = arxiv_filter._load_inference()

        assert func1 is fake_func
        assert func2 is fake_func
        assert func1 is func2

    def test_load_inference_force_reload(self, temp_plugin_dir):
        """测试强制重新加载：清除缓存并重新从模块读取函数"""
        fake_module = types.ModuleType("plugins.arxiv_filter.arxiv_inference")
        fake_func1 = lambda **kwargs: "ok1"  # noqa: E731
        fake_func2 = lambda **kwargs: "ok2"  # noqa: E731
        fake_module.get_positive_arxiv_today_as_string = fake_func1

        arxiv_filter._inference_func = None
        with patch.dict(sys.modules, {"plugins.arxiv_filter.arxiv_inference": fake_module}):
            func1 = arxiv_filter._load_inference()
            assert func1 is fake_func1

            # 模拟模块内容变更，然后 force_reload
            fake_module.get_positive_arxiv_today_as_string = fake_func2
            with patch("importlib.reload", side_effect=lambda m: m):
                func2 = arxiv_filter._load_inference(force_reload=True)

        assert func2 is fake_func2
        assert func2 is not func1


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
        today = arxiv_filter._business_now(mock_context).date().isoformat()

        with patch.object(
            arxiv_filter,
            "_run_filter",
            new=AsyncMock(return_value=arxiv_filter.segments("Papers found")),
        ):
            with patch.object(arxiv_filter, "run_sync", return_value=today):
                result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
                # 应该调用 _run_filter
                assert result is not None
        assert arxiv_filter._should_send_today(str(mock_context.plugin_dir)) is False

    @pytest.mark.asyncio
    async def test_check_arxiv_update_failure_does_not_mark_sent(self, mock_context):
        today = arxiv_filter._business_now(mock_context).date().isoformat()

        with patch.object(
            arxiv_filter,
            "_run_filter",
            new=AsyncMock(
                return_value=arxiv_filter.segments("❌ 论文筛选服务暂时不可用，请稍后再试。")
            ),
        ):
            with patch.object(arxiv_filter, "run_sync", return_value=today):
                result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)

        assert "暂时不可用" in str(result)
        assert arxiv_filter._should_send_today(str(mock_context.plugin_dir)) is True

    @pytest.mark.asyncio
    async def test_check_arxiv_update_not_updated_yet(self, mock_context):
        """测试 arXiv 尚未更新"""
        # 返回昨天的日期
        yesterday = "2026-01-01"

        with patch.object(arxiv_filter, "run_sync", return_value=yesterday):
            result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
            # 应该返回空列表
            assert result == []

    @pytest.mark.asyncio
    async def test_check_arxiv_final_check_no_update(self, mock_context):
        """测试最后检查仍未更新"""
        # 返回旧日期
        old_date = "2026-01-01"
        with patch.object(arxiv_filter, "run_sync", return_value=old_date):
            result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=True)
            assert result is not None
            result_text = str(result)
            assert "暂未更新" in result_text or "停更" in result_text

    @pytest.mark.asyncio
    async def test_check_arxiv_update_error(self, mock_context):
        """测试检查更新时出错"""
        with patch.object(arxiv_filter, "run_sync", side_effect=Exception("Network error")):
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
        with patch.object(
            arxiv_filter,
            "_run_filter",
            new=AsyncMock(return_value=arxiv_filter.segments("scheduled")),
        ) as mock_run:
            result = await arxiv_filter.scheduled(mock_context)
            assert result is not None
            mock_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheduled_check(self, mock_context):
        """测试定时检查任务"""
        with patch.object(
            arxiv_filter, "_check_arxiv_update", new=AsyncMock(return_value=[])
        ) as mock_check:
            await arxiv_filter.scheduled_check(mock_context)
            mock_check.assert_called_once_with(mock_context, is_final_check=False)

    @pytest.mark.asyncio
    async def test_scheduled_final_check(self, mock_context):
        """测试最后检查任务"""
        with patch.object(
            arxiv_filter, "_check_arxiv_update", new=AsyncMock(return_value=[])
        ) as mock_check:
            await arxiv_filter.scheduled_final_check(mock_context)
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
        with patch.object(
            arxiv_filter, "_load_inference", return_value=lambda **kwargs: mock_result
        ):
            result = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            result_text = str(result)
            assert (
                "First Paper Title" in result_text
                or "Second Paper Title" in result_text
                or "论文" in result_text
            )

    def test_extract_arxiv_links_normalizes_and_dedupes(self):
        text = """
Link       : https://arxiv.org/abs/2605.16917v1
PDF        : https://arxiv.org/pdf/2605.16917
Link       : https://arxiv.org/abs/2605.18050
"""
        assert arxiv_codex_summary.extract_arxiv_links(text) == [
            "https://arxiv.org/abs/2605.16917",
            "https://arxiv.org/abs/2605.18050",
        ]

    @pytest.mark.asyncio
    async def test_run_filter_schedules_codex_summary_with_positive_links(self, mock_context):
        mock_result = """
----- Positive #1 -----
Title      : First Paper Title
Link       : https://arxiv.org/abs/2605.16917
Probability: 0.9000

----- Positive #2 -----
Title      : Second Paper Title
Link       : https://arxiv.org/abs/2605.18050
Probability: 0.7500
"""
        with patch.object(
            arxiv_filter, "_load_inference", return_value=lambda **kwargs: mock_result
        ):
            with patch.object(
                arxiv_filter, "schedule_codex_summary_from_filter_result"
            ) as schedule_mock:
                result = await arxiv_filter._run_filter(
                    mock_context,
                    allow_codex_sidecar=True,
                )

        assert "First Paper Title" in str(result)
        schedule_mock.assert_called_once_with(
            mock_context,
            date=arxiv_filter._business_now(mock_context).date().isoformat(),
            filter_text=mock_result,
        )

    @pytest.mark.asyncio
    async def test_run_filter_keeps_paper_list_when_codex_schedule_fails(self, mock_context):
        mock_result = """
----- Positive #1 -----
Title      : First Paper Title
Link       : https://arxiv.org/abs/2605.16917
Probability: 0.9000
"""
        with patch.object(
            arxiv_filter, "_load_inference", return_value=lambda **kwargs: mock_result
        ):
            with patch.object(
                arxiv_filter,
                "schedule_codex_summary_from_filter_result",
                side_effect=RuntimeError("queue unavailable"),
            ):
                result = await arxiv_filter._run_filter(mock_context)

        assert "First Paper Title" in str(result)

    @pytest.mark.asyncio
    async def test_codex_summary_missing_codex_does_not_raise(self, mock_context):
        mock_context.send_action = AsyncMock()
        service = Mock()
        service.enqueue_or_replay = AsyncMock(side_effect=RuntimeError("codex unavailable"))
        mock_context.capabilities = PluginCapabilities(codex_arxiv_summary=service)
        task = arxiv_codex_summary.schedule_codex_summary(
            mock_context,
            date="2026-05-19",
            links=["https://arxiv.org/abs/2605.16917"],
        )

        assert task is not None
        await task
        service.enqueue_or_replay.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("principal", "is_system"),
        [
            pytest.param(
                PluginPrincipal(
                    kind="user",
                    user_id=123,
                    is_bot_admin=True,
                    is_private=True,
                    delivery_targets=(DeliveryTarget("private", 123),),
                ),
                False,
                id="private-chat",
            ),
            pytest.param(
                PluginPrincipal(
                    kind="user",
                    user_id=123,
                    group_id=456,
                    is_bot_admin=True,
                    delivery_targets=(DeliveryTarget("group", 456),),
                ),
                False,
                id="group-chat",
            ),
            pytest.param(
                PluginPrincipal(
                    kind="scheduled_system",
                    delivery_targets=(DeliveryTarget("group", 789),),
                ),
                True,
                id="scheduler-signed-group",
            ),
        ],
    )
    async def test_codex_summary_uses_only_core_signed_delivery_targets(
        self,
        mock_context,
        principal,
        is_system,
    ):
        mock_context.send_action = AsyncMock()
        service = Mock()
        service.enqueue_or_replay = AsyncMock(return_value="queued")
        mock_context.principal = principal
        mock_context.capabilities = PluginCapabilities(
            is_system=is_system,
            codex_arxiv_summary=service,
        )
        task = arxiv_codex_summary.schedule_codex_summary(
            mock_context,
            date="2026-07-10",
            links=["https://arxiv.org/abs/2607.00001"],
        )
        assert task is not None
        await task

        service.enqueue_or_replay.assert_awaited_once_with(
            date="2026-07-10",
            links=["https://arxiv.org/abs/2607.00001"],
        )

    @pytest.mark.asyncio
    async def test_scheduled_zero_targets_skip_before_claim_or_inference(
        self,
        mock_context,
    ):
        mock_context.principal = PluginPrincipal(kind="scheduled_system", delivery_targets=())
        mock_context.capabilities = PluginCapabilities(is_system=True)

        with patch.object(
            arxiv_filter,
            "_claim_send_today",
            side_effect=AssertionError("zero-target schedule must not claim"),
        ):
            assert await arxiv_filter.scheduled_check(mock_context) == []
            assert await arxiv_filter.scheduled_final_check(mock_context) == []
            assert await arxiv_filter.scheduled(mock_context) == []

    @pytest.mark.asyncio
    async def test_filter_inference_is_singleflight_and_cached_per_business_day(self, mock_context):
        calls = 0
        gate = threading.Event()

        def inference(**_kwargs):
            nonlocal calls
            calls += 1
            gate.wait(1)
            return "No positive predictions"

        arxiv_filter._FILTER_CACHE.clear()
        with patch.object(arxiv_filter, "_load_inference", return_value=inference):
            first = asyncio.create_task(arxiv_filter._run_filter(mock_context))
            second = asyncio.create_task(arxiv_filter._run_filter(mock_context))
            await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(first, second)
            await arxiv_filter._run_filter(mock_context)

        assert calls == 1


def test_arxiv_daily_claim_is_atomic_and_releasable(temp_plugin_dir):
    business_date = "2030-01-02"
    plugin_dir = str(temp_plugin_dir)

    assert arxiv_filter._claim_send_today(plugin_dir, business_date) is True
    assert arxiv_filter._claim_send_today(plugin_dir, business_date) is False
    arxiv_filter._release_claim(plugin_dir, business_date)
    assert arxiv_filter._claim_send_today(plugin_dir, business_date) is True


def test_arxiv_training_cache_publishes_only_completed_results(monkeypatch, tmp_path):
    with patch.dict(sys.modules, {"feedparser": SimpleNamespace()}):
        module = importlib.import_module(
            "plugins.arxiv_filter.train_model.data_prep.step2_fetch_all_astro_ph"
        )
    monkeypatch.setattr(module, "MONTHLY_DIR", tmp_path)
    result = module.FetchResult(
        papers=[{"arxiv_id": "2607.00001", "title": "t", "abstract": "a"}],
        completed=False,
        next_offset=2000,
        total_results=4000,
    )

    module.save_checkpoint(2607, result)

    assert module.API_URL.startswith("https://")
    assert "@" not in module.HEADERS["User-Agent"]
    assert module.load_cache(2607) is None
    checkpoint = module.load_checkpoint(2607)
    assert checkpoint["completed"] is False
    assert checkpoint["next_offset"] == 2000


def test_arxiv_run_all_uses_script_directory_and_current_python(monkeypatch):
    module = importlib.import_module("plugins.arxiv_filter.train_model.data_prep.run_all")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "time", Mock(side_effect=[0.0, 1.0]))

    assert module.run_step("step1_extract_positive_ids.py", "step") is True
    assert captured["args"][0] == sys.executable
    assert Path(captured["args"][1]).is_absolute()
    assert captured["cwd"] == str(module.SCRIPT_DIR)


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


class TestInferenceBackendCaching:
    def test_transformers_backend_caches_model_objects(self, monkeypatch, tmp_path):
        pytest.importorskip("torch")
        pytest.importorskip("transformers")
        backend = importlib.import_module("plugins.arxiv_filter.inference.transformers_backend")
        backend._MODEL_CACHE.clear()

        class DummyModel:
            def to(self, _device):
                return self

            def eval(self):
                return self

        model_loader = Mock(return_value=DummyModel())
        tokenizer_loader = Mock(return_value=object())
        monkeypatch.setattr(
            backend,
            "AutoModelForSequenceClassification",
            SimpleNamespace(from_pretrained=model_loader),
        )
        monkeypatch.setattr(
            backend,
            "AutoTokenizer",
            SimpleNamespace(from_pretrained=tokenizer_loader),
        )

        device = backend.torch.device("cpu")
        first = backend.load_model_and_tokenizer(str(tmp_path), device)
        second = backend.load_model_and_tokenizer(str(tmp_path), device)

        assert first == second
        assert model_loader.call_count == 1
        assert tokenizer_loader.call_count == 1

    def test_knn_backend_caches_runtime_model(self, monkeypatch, tmp_path):
        pytest.importorskip("torch")
        pd = pytest.importorskip("pandas")
        backend = importlib.import_module("plugins.arxiv_filter.inference.knn_backend")
        shared = importlib.import_module("plugins.arxiv_filter.inference.shared")
        backend._MODEL_CACHE.clear()

        class DummyModel:
            def predict_proba(self, _data, input_mode="title_abstract"):
                return backend.np.array([0.9], dtype=backend.np.float32)

        constructor = Mock(return_value=DummyModel())
        monkeypatch.setattr(backend, "KNNInferenceModel", constructor)

        params = shared.InferenceParams(
            model_path=str(tmp_path),
            threshold=0.5,
            batch_size=32,
            max_len=64,
            input_mode="title_only",
            model_type="knn",
        )
        data = pd.DataFrame([{"Title": "Paper"}])

        backend.run_knn_inference(params, data)
        backend.run_knn_inference(params, data)

        assert constructor.call_count == 1

    def test_multi_interest_backend_caches_runtime_model(self, monkeypatch, tmp_path):
        pytest.importorskip("torch")
        pd = pytest.importorskip("pandas")
        backend = importlib.import_module("plugins.arxiv_filter.inference.multi_interest_backend")
        shared = importlib.import_module("plugins.arxiv_filter.inference.shared")
        backend._MODEL_CACHE.clear()

        class DummyModel:
            def predict_proba(self, _data, input_mode="title_abstract"):
                return backend.np.array([0.9], dtype=backend.np.float32)

        constructor = Mock(return_value=DummyModel())
        monkeypatch.setattr(backend, "MultiInterestInferenceModel", constructor)

        params = shared.InferenceParams(
            model_path=str(tmp_path),
            threshold=0.5,
            batch_size=32,
            max_len=64,
            input_mode="title_only",
            model_type="multi_interest",
        )
        data = pd.DataFrame([{"Title": "Paper"}])

        backend.run_multi_interest_inference(params, data)
        backend.run_multi_interest_inference(params, data)

        assert constructor.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
