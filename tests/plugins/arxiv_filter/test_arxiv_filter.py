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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.interfaces import DeliveryTarget, PluginCapabilities, PluginPrincipal
from core.public_errors import PUBLIC_ERROR_PREFIX
from tests.helpers.arxiv_filter_test_support import (
    ArxivWorld,
    install_fake_encoder,
    use_plugin_config,
    write_knn_model,
)
from tests.helpers.settings_snapshot import with_settings_reader

_RUNNER = "plugins.arxiv_filter.inference.runner"

arxiv_filter        = importlib.import_module("plugins.arxiv_filter.main")
arxiv_codex_summary = importlib.import_module("plugins.arxiv_filter.codex_summary")
arxiv_filter_utils  = importlib.import_module("plugins.arxiv_filter.utils")


# ============================================================
# Fixtures
# ============================================================


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
            self.data_dir   = plugin_dir / "data"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.config = {}
            self.principal = PluginPrincipal(kind="lifecycle")
            self.capabilities = PluginCapabilities()
            self.state: dict  = {}

    return with_settings_reader(MockContext(temp_plugin_dir))


@pytest.fixture
def mock_event():
    """模拟事件"""
    return {"user_id": 12345, "group_id": 123456789, "message_type": "group"}


def _today(context) -> str:
    return arxiv_filter._business_now(context).date().isoformat()


async def _drain_codex_sidecar(context) -> None:
    """等待筛选结果派生出的 Codex 摘要后台任务结束。"""

    await asyncio.gather(*list(context.state.get("arxiv_background_tasks", ())))


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

    def test_get_status_file_path(self, temp_data_dir):
        """测试获取状态文件路径"""
        status_path = arxiv_filter._get_status_file_path(temp_data_dir)
        expected    = temp_data_dir / "update_status.json"
        assert status_path == expected

    def test_load_status_creates_default(self, temp_data_dir):
        """测试加载状态时创建默认状态"""
        status = arxiv_filter._load_update_status(temp_data_dir)
        assert status == {}

    def test_save_and_load_status(self, temp_data_dir):
        """测试保存和加载状态"""
        test_status = {"last_sent_date": "2026-02-04", "last_sent_time": "2026-02-04T10:00:00"}
        arxiv_filter._save_update_status(temp_data_dir, test_status)

        loaded = arxiv_filter._load_update_status(temp_data_dir)
        assert loaded["last_sent_date"] == "2026-02-04"
        assert loaded["last_sent_time"] == "2026-02-04T10:00:00"

    @pytest.mark.parametrize(
        "payload",
        ["{broken", "[]", '{"last_sent_date": 123}', '{"last_sent_date": "not-a-date"}'],
    )
    def test_corrupt_status_fails_closed(self, temp_data_dir, payload):
        """损坏状态不能退化为“今天尚未发送”，否则定时任务可能重复群发。"""

        status_path = arxiv_filter._get_status_file_path(temp_data_dir)
        status_path.write_text(payload, encoding="utf-8")
        business_date = "2026-08-06"

        with pytest.raises(arxiv_filter._StatusFileError):
            arxiv_filter._load_update_status(temp_data_dir)
        with pytest.raises(arxiv_filter._StatusFileError):
            arxiv_filter._claim_send_today(temp_data_dir, business_date)
        assert not arxiv_filter._claim_path(temp_data_dir, business_date).exists()

    def test_should_send_today_new_day(self, temp_data_dir):
        """测试检查是否应该发送（新的一天）"""
        status = {"last_sent_date": "2026-01-01"}
        arxiv_filter._save_update_status(temp_data_dir, status)

        # 应该返回 True（因为日期不同）
        result = arxiv_filter._should_send_today(
            temp_data_dir,
            arxiv_filter._business_now().date().isoformat(),
        )
        assert result is True

    def test_should_send_today_already_sent(self, temp_data_dir):
        """测试今天已经发送过"""
        today  = arxiv_filter._business_now().date().isoformat()
        status = {"last_sent_date": today}
        arxiv_filter._save_update_status(temp_data_dir, status)

        result = arxiv_filter._should_send_today(
            temp_data_dir,
            arxiv_filter._business_now().date().isoformat(),
        )
        assert result is False

    def test_mark_sent_today(self, temp_data_dir):
        """测试标记今天已发送"""
        arxiv_filter._mark_sent_today(
            temp_data_dir,
            arxiv_filter._business_now().date().isoformat(),
        )

        today  = arxiv_filter._business_now().date().isoformat()
        status = arxiv_filter._load_update_status(temp_data_dir)
        assert status["last_sent_date"] == today
        assert "last_sent_time" in status


# ============================================================
# Test Handle
# ============================================================


class TestHandle:
    """测试命令处理"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["help", "帮助"], ids=["english", "chinese"])
    async def test_handle_help(self, mock_context, mock_event, args):
        """help 与 帮助 两种写法都返回帮助信息"""
        result = await arxiv_filter.handle("arxiv", args, mock_event, mock_context)
        assert result is not None
        result_text = str(result)
        assert "arXiv" in result_text or "论文" in result_text

    @pytest.mark.asyncio
    async def test_handle_unknown_command_echo_is_bounded(self, mock_context, mock_event):
        unknown = "x" * 200

        result = await arxiv_filter.handle("arxiv", unknown, mock_event, mock_context)

        result_text = str(result)
        assert len(result_text) < 200
        assert "未知命令" in result_text

    @pytest.mark.asyncio
    async def test_handle_default_returns_papers_for_the_listing_date(
        self, mock_context, mock_event, tmp_path, monkeypatch
    ):
        """默认命令按 arXiv 页面上的列表日期筛选并回复论文"""
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06", titles=["Pulsar timing"])

        result = await arxiv_filter.handle("arxiv", "", mock_event, mock_context)

        assert result is not None
        result_text = str(result)
        assert "Pulsar timing" in result_text
        assert "2026-08-06" in result_text

    @pytest.mark.asyncio
    async def test_handle_enables_codex_sidecar_only_when_core_grants_it(
        self,
        mock_context,
        mock_event,
        tmp_path,
        monkeypatch,
    ):
        # 能不能附带 Codex 摘要由 Core 按 /codex 的声明决定，插件只看能力是否发出。
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06", titles=["Pulsar timing"])
        service = Mock()
        service.enqueue_or_replay = AsyncMock(return_value="queued")
        mock_context.principal = PluginPrincipal(kind="user", user_id=12345, group_id=123456789)
        mock_context.capabilities = PluginCapabilities(
            is_bot_admin        = True,
            codex_arxiv_summary = service,
        )

        await arxiv_filter.handle("arxiv", "", mock_event, mock_context)
        await _drain_codex_sidecar(mock_context)

        service.enqueue_or_replay.assert_awaited_once_with(
            date  = "2026-08-06",
            links = ["https://arxiv.org/abs/2607.00001"],
        )

        mock_context.state = {}
        mock_context.capabilities = PluginCapabilities(is_bot_admin=True)
        await arxiv_filter.handle("arxiv", "", mock_event, mock_context)

        assert not mock_context.state.get("arxiv_background_tasks")
        service.enqueue_or_replay.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_exception(self, mock_context, mock_event, tmp_path, monkeypatch):
        """筛选抛出异常时只回复公开错误提示"""
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06")
        use_plugin_config(monkeypatch, tmp_path, {"model": "not-an-object"})

        result = await arxiv_filter.handle("arxiv", "", mock_event, mock_context)

        assert result is not None
        result_text = str(result)
        assert PUBLIC_ERROR_PREFIX in result_text


# ============================================================
# Test Run Filter
# ============================================================


class TestRunFilter:
    """测试论文筛选功能"""

    @pytest.mark.asyncio
    async def test_run_filter_model_not_loaded(
        self, mock_context, mock_event, tmp_path, monkeypatch
    ):
        """推理模块的可选依赖缺失时提示无法加载模型"""
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06")
        monkeypatch.setitem(sys.modules, "pandas", None)
        monkeypatch.delitem(sys.modules, _RUNNER, raising=False)

        result = await arxiv_filter._run_filter(mock_context)

        assert result is not None
        result_text = str(result)
        assert "无法加载AI模型" in result_text or "模型" in result_text

    @pytest.mark.asyncio
    async def test_run_filter_rejects_unknown_inference_result(self, mock_context, mock_event):
        """未知推理文本不能被当成可提交的成功结果。"""
        # 创建一个没有模型目录的上下文
        empty_dir = mock_context.plugin_dir / "empty"
        empty_dir.mkdir()

        # 模拟模型函数存在但路径不存在
        # 注: _run_filter 通过 load_plugin_config() 加载配置（基于 __file__），
        #     不直接依赖 context.plugin_dir 来解析模型路径。
        #     因此这里直接验证推理输出协议，而不是目录存在性。
        with patch.object(arxiv_filter, "_load_inference", return_value=lambda **kwargs: "result"):
            mock_context.plugin_dir = empty_dir
            result                  = await arxiv_filter._run_filter(mock_context)
            assert result is not None
            assert result.succeeded is False
            assert result.outcome == "unknown_result"

    @pytest.mark.asyncio
    async def test_run_filter_no_papers(self, mock_context, mock_event, tmp_path, monkeypatch):
        """测试没有符合条件的论文"""
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06")

        result = await arxiv_filter._run_filter(mock_context)

        assert result is not None
        result_text = str(result)
        assert "暂时没有发现感兴趣的论文" in result_text or "没有" in result_text

    @pytest.mark.parametrize(
        ("text", "kind", "successful"),
        [
            ("\n----- Positive #1 -----\nTitle      : x", "papers", True),
            ("No positive predictions found.", "no_positive_predictions", True),
            ("No papers found.", "no_papers", False),
            ("Error: Network error", "unknown", False),
        ],
    )
    def test_inference_text_classification(self, text, kind, successful):
        assert arxiv_filter._classify_inference_text(text) == kind
        assert arxiv_filter._is_successful_inference(text) is successful

    @pytest.mark.asyncio
    async def test_run_filter_without_paper_list_is_a_failure(
        self, mock_context, mock_event, tmp_path, monkeypatch
    ):
        """源列表抓不到论文时不是成功结果，不能提交当日投递。"""
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06", titles=())

        result = await arxiv_filter._run_filter(mock_context)

        assert result.succeeded is False
        assert result.outcome == "inference_error"
        assert PUBLIC_ERROR_PREFIX in str(result)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scenario", "outcome"),
        [
            ("file-not-found", "model_file_error"),
            ("import-error", "dependency_error"),
            ("runtime-error", "runtime_error"),
        ],
    )
    async def test_run_filter_backend_exception_returns_unexpected_error(
        self, mock_context, mock_event, tmp_path, monkeypatch, scenario, outcome
    ):
        """模型文件缺失、依赖缺失和其他运行异常都返回统一的意外错误提示"""
        world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06")
        if scenario == "file-not-found":
            (world.model_dir / "meta.json").unlink()
        elif scenario == "import-error":
            monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        else:
            world.encoder.encode_error = RuntimeError("Unexpected error")

        result = await arxiv_filter._run_filter(mock_context)

        assert result is not None
        assert result.outcome == outcome
        result_text = str(result)
        assert PUBLIC_ERROR_PREFIX in result_text


# ============================================================
# Test Inference Loading
# ============================================================


class TestInferenceLoading:
    """测试推理模块加载"""

    def test_load_inference_caches_result(self, monkeypatch):
        """测试推理函数缓存：第二次加载不再重新导入推理模块"""
        monkeypatch.setattr(arxiv_filter, "_inference_func", None)

        func1 = arxiv_filter._load_inference()
        # 缓存失效时会重新执行推理模块，得到另一个函数对象。
        monkeypatch.delitem(sys.modules, _RUNNER)
        func2 = arxiv_filter._load_inference()

        assert func1 is func2
        assert func1.__name__ == "get_positive_arxiv_today_as_string"

    def test_plugin_init_clears_inference_cache(self, monkeypatch):
        """插件代加载清除函数缓存，由 core 负责模块代际重载。"""
        monkeypatch.setattr(arxiv_filter, "_inference_func", None)

        func1 = arxiv_filter._load_inference()
        # 模拟模块内容变更，然后由插件 init 清除函数缓存。
        monkeypatch.delitem(sys.modules, _RUNNER)
        arxiv_filter.init()
        func2 = arxiv_filter._load_inference()

        assert func2 is not func1
        assert func2 is sys.modules[_RUNNER].get_positive_arxiv_today_as_string


# ============================================================
# Test Check ArXiv Update
# ============================================================


class TestCheckArxivUpdate:
    """测试 arXiv 更新检查"""

    @pytest.mark.asyncio
    async def test_check_arxiv_update_already_sent(self, mock_context):
        """测试今天已经发送过"""
        # 标记今天已发送
        arxiv_filter._mark_sent_today(
            mock_context.data_dir,
            arxiv_filter._business_now(mock_context).date().isoformat(),
        )

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)
        assert result == []

    @pytest.mark.asyncio
    async def test_check_arxiv_update_updated_today(self, mock_context, tmp_path, monkeypatch):
        """测试 arXiv 已更新到今天"""
        # 确保今天未发送
        today = _today(mock_context)
        ArxivWorld(monkeypatch, tmp_path, list_date=today, titles=["Pulsar timing"])

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)

        assert result is not None
        assert "Pulsar timing" in str(result)
        assert (
            arxiv_filter._should_send_today(
                mock_context.data_dir,
                today,
            )
            is True
        )
        await result.delivery_receipt.record(True)
        assert (
            arxiv_filter._should_send_today(
                mock_context.data_dir,
                today,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_check_arxiv_update_failure_does_not_mark_sent(
        self, mock_context, tmp_path, monkeypatch
    ):
        today = _today(mock_context)
        ArxivWorld(monkeypatch, tmp_path, list_date=today, titles=())

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)

        assert PUBLIC_ERROR_PREFIX in str(result)
        assert (
            arxiv_filter._should_send_today(
                mock_context.data_dir,
                today,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_check_arxiv_update_not_updated_yet(self, mock_context, tmp_path, monkeypatch):
        """测试 arXiv 尚未更新"""
        # 页面上还是旧日期
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-01-01")

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)

        # 应该返回空列表
        assert result == []

    @pytest.mark.asyncio
    async def test_check_arxiv_final_check_no_update(self, mock_context, tmp_path, monkeypatch):
        """测试最后检查仍未更新"""
        # 页面上还是旧日期
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-01-01")

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=True)

        assert result is not None
        result_text = str(result)
        assert "暂未更新" in result_text or "停更" in result_text

    @pytest.mark.asyncio
    async def test_check_arxiv_update_error(self, mock_context, tmp_path, monkeypatch):
        """测试检查更新时出错"""
        world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-01-01")
        world.request_error = RuntimeError("Network error")

        result = await arxiv_filter._check_arxiv_update(mock_context, is_final_check=False)

        assert result == []


# ============================================================
# Test Scheduled Tasks
# ============================================================


class TestScheduledTasks:
    """测试定时任务"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("entry", "sends_stop_notice"),
        [("scheduled_check", False), ("scheduled_final_check", True)],
    )
    async def test_scheduled_entries_run_update_check(
        self, mock_context, tmp_path, monkeypatch, entry, sends_stop_notice
    ):
        """页面仍是旧日期：定时检查继续等待，仅最后一次检查发停更通知"""
        ArxivWorld(monkeypatch, tmp_path, list_date="2026-01-01")

        result = await getattr(arxiv_filter, entry)(mock_context)

        if sends_stop_notice:
            assert "暂未更新" in str(result)
        else:
            assert result == []


# ============================================================
# Test Multiple Papers
# ============================================================


class TestMultiplePapers:
    """测试多论文场景"""

    @pytest.mark.asyncio
    async def test_multiple_papers_output(self, mock_context, mock_event, tmp_path, monkeypatch):
        """测试多论文输出格式"""
        ArxivWorld(
            monkeypatch,
            tmp_path,
            list_date = "2026-08-06",
            titles    = ["First Pulsar Paper Title", "Second Pulsar Paper Title"],
        )

        result = await arxiv_filter._run_filter(mock_context)

        assert result is not None
        result_text = str(result)
        assert "First Pulsar Paper Title" in result_text
        assert "Second Pulsar Paper Title" in result_text

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
    async def test_run_filter_schedules_codex_summary_with_positive_links(
        self, mock_context, tmp_path, monkeypatch
    ):
        ArxivWorld(
            monkeypatch,
            tmp_path,
            list_date = "2026-05-19",
            titles    = ["First Pulsar Paper Title", "Second Pulsar Paper Title"],
        )
        service = Mock()
        service.enqueue_or_replay = AsyncMock(return_value="queued")
        mock_context.capabilities = PluginCapabilities(codex_arxiv_summary=service)

        result = await arxiv_filter._run_filter(
            mock_context,
            allow_codex_sidecar = True,
            source_date         = "2026-05-19",
        )
        await _drain_codex_sidecar(mock_context)

        assert "First Pulsar Paper Title" in str(result)
        service.enqueue_or_replay.assert_awaited_once_with(
            date  = "2026-05-19",
            links = ["https://arxiv.org/abs/2607.00001", "https://arxiv.org/abs/2607.00002"],
        )

    @pytest.mark.asyncio
    async def test_run_filter_keeps_paper_list_when_codex_schedule_fails(
        self, mock_context, tmp_path, monkeypatch
    ):
        ArxivWorld(
            monkeypatch, tmp_path, list_date="2026-05-19", titles=["First Pulsar Paper Title"]
        )
        service = Mock()
        service.enqueue_or_replay = AsyncMock(side_effect=RuntimeError("queue unavailable"))
        mock_context.capabilities = PluginCapabilities(codex_arxiv_summary=service)

        result = await arxiv_filter._run_filter(
            mock_context,
            allow_codex_sidecar = True,
            source_date         = "2026-05-19",
        )
        await _drain_codex_sidecar(mock_context)

        assert "First Pulsar Paper Title" in str(result)
        service.enqueue_or_replay.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_filter_does_not_guess_codex_date_when_source_date_is_unknown(
        self,
        mock_context,
        tmp_path,
        monkeypatch,
    ):
        ArxivWorld(
            monkeypatch, tmp_path, list_date="2026-05-19", titles=["First Pulsar Paper Title"]
        )
        service = Mock()
        service.enqueue_or_replay = AsyncMock(return_value="queued")
        mock_context.capabilities = PluginCapabilities(codex_arxiv_summary=service)

        result = await arxiv_filter._run_filter(
            mock_context,
            allow_codex_sidecar = True,
            source_date         = None,
        )
        await _drain_codex_sidecar(mock_context)

        assert "日期未能确认" in str(result)
        service.enqueue_or_replay.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_codex_summary_missing_codex_does_not_raise(self, mock_context):
        service = Mock()
        service.enqueue_or_replay = AsyncMock(side_effect=RuntimeError("codex unavailable"))
        mock_context.capabilities = PluginCapabilities(codex_arxiv_summary=service)
        task = arxiv_codex_summary.schedule_codex_summary(
            mock_context,
            date  = "2026-05-19",
            links = ["https://arxiv.org/abs/2605.16917"],
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
                    kind             = "user",
                    user_id          = 123,
                    is_bot_admin     = True,
                    is_private       = True,
                    delivery_targets = (DeliveryTarget("private", 123),),
                ),
                False,
                id="private-chat",
            ),
            pytest.param(
                PluginPrincipal(
                    kind             = "user",
                    user_id          = 123,
                    group_id         = 456,
                    is_bot_admin     = True,
                    delivery_targets = (DeliveryTarget("group", 456),),
                ),
                False,
                id="group-chat",
            ),
            pytest.param(
                PluginPrincipal(
                    kind             = "scheduled_system",
                    delivery_targets = (DeliveryTarget("group", 789),),
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
        service = Mock()
        service.enqueue_or_replay = AsyncMock(return_value="queued")
        mock_context.principal    = principal
        mock_context.capabilities = PluginCapabilities(
            is_system           = is_system,
            codex_arxiv_summary = service,
        )
        task = arxiv_codex_summary.schedule_codex_summary(
            mock_context,
            date  = "2026-07-10",
            links = ["https://arxiv.org/abs/2607.00001"],
        )
        assert task is not None
        await task

        service.enqueue_or_replay.assert_awaited_once_with(
            date  = "2026-07-10",
            links = ["https://arxiv.org/abs/2607.00001"],
        )

    @pytest.mark.asyncio
    async def test_scheduled_zero_targets_skip_before_claim_or_inference(
        self,
        mock_context,
        tmp_path,
        monkeypatch,
    ):
        world = ArxivWorld(monkeypatch, tmp_path, list_date=_today(mock_context))
        mock_context.principal = PluginPrincipal(kind="scheduled_system", delivery_targets=())
        mock_context.capabilities = PluginCapabilities(is_system=True)

        assert await arxiv_filter.scheduled_check(mock_context) == []
        assert await arxiv_filter.scheduled_final_check(mock_context) == []

        assert world.requests == []
        assert list(mock_context.data_dir.glob("claim-*")) == []

    @pytest.mark.asyncio
    async def test_filter_inference_is_singleflight_and_cached_per_source_date(
        self, mock_context, tmp_path, monkeypatch
    ):
        world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-06")
        gate = threading.Event()
        # 推理在抓取 arXiv 页面时被挡住，期间并发的两次筛选必须共用同一次推理。
        world.request_barrier = lambda: gate.wait(1)

        first = asyncio.create_task(
            arxiv_filter._run_filter(mock_context, source_date="2026-08-06")
        )
        second = asyncio.create_task(
            arxiv_filter._run_filter(mock_context, source_date="2026-08-06")
        )
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(first, second)
        await arxiv_filter._run_filter(mock_context, source_date="2026-08-06")

        assert len(world.requests) == 1


def test_arxiv_daily_claim_is_atomic_and_releasable(temp_data_dir):
    business_date = "2030-01-02"

    assert arxiv_filter._claim_send_today(temp_data_dir, business_date) is True
    assert arxiv_filter._claim_send_today(temp_data_dir, business_date) is False
    arxiv_filter._release_claim(temp_data_dir, business_date)
    assert arxiv_filter._claim_send_today(temp_data_dir, business_date) is True


def test_arxiv_training_cache_publishes_only_completed_results(monkeypatch, tmp_path):
    with patch.dict(sys.modules, {"feedparser": SimpleNamespace()}):
        module = importlib.import_module(
            "plugins.arxiv_filter.train_model.data_prep.step2_fetch_all_astro_ph"
        )
    monkeypatch.setattr(module, "MONTHLY_DIR", tmp_path)
    result = module.FetchResult(
        papers        = [{"arxiv_id": "2607.00001", "title": "t", "abstract": "a"}],
        completed     = False,
        next_offset   = 2000,
        total_results = 4000,
    )

    module.save_checkpoint(2607, result)

    assert module.load_cache(2607) is None
    checkpoint = module.load_checkpoint(2607)
    assert checkpoint.completed is False
    assert checkpoint.next_offset == 2000
    assert checkpoint.total_results == 4000


def test_arxiv_run_all_uses_script_directory_and_current_python(monkeypatch):
    module   = importlib.import_module("plugins.arxiv_filter.train_model.data_prep.run_all")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.time, "perf_counter", Mock(side_effect=[0.0, 1.0]))

    assert module.run_step("step1_extract_positive_ids.py", "step") is True
    assert captured["args"][0] == sys.executable
    assert Path(captured["args"][1]).is_absolute()
    assert captured["cwd"] == str(module.SCRIPT_DIR)


class TestInferenceBackendCaching:
    def test_transformers_backend_caches_model_objects(self, monkeypatch, tmp_path):
        pytest.importorskip("torch")
        pytest.importorskip("transformers")
        backend = importlib.import_module("plugins.arxiv_filter.inference.transformers_backend")
        backend._MODEL_CACHE.clear()
        artifact = tmp_path / "artifact.json"
        artifact.write_text('{"version": 1}', encoding="utf-8")

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
        first  = backend.load_model_and_tokenizer(str(tmp_path), device)
        second = backend.load_model_and_tokenizer(str(tmp_path), device)
        artifact.write_text('{"version": 2}', encoding="utf-8")
        third = backend.load_model_and_tokenizer(str(tmp_path), device)

        assert first == second
        assert third == first
        assert model_loader.call_count == 2
        assert tokenizer_loader.call_count == 2

    def test_knn_backend_caches_runtime_model(self, monkeypatch, tmp_path):
        pytest.importorskip("torch")
        pd      = pytest.importorskip("pandas")
        backend = importlib.import_module("plugins.arxiv_filter.inference.knn_backend")
        shared  = importlib.import_module("plugins.arxiv_filter.inference.shared")
        backend._MODEL_CACHE.clear()
        write_knn_model(tmp_path)
        artifact = tmp_path / "artifact.json"
        artifact.write_text('{"version": 1}', encoding="utf-8")
        encoder = install_fake_encoder(monkeypatch)

        params = shared.InferenceParams(
            model_path = str(tmp_path),
            threshold  = 0.5,
            batch_size = 32,
            max_len    = 64,
            input_mode = "title_only",
            model_type = "knn",
        )
        data = pd.DataFrame([{"Title": "Paper"}])

        backend.run_knn_inference(params, data)
        backend.run_knn_inference(params, data)
        artifact.write_text('{"version": 2}', encoding="utf-8")
        backend.run_knn_inference(params, data)

        # 每构造一次模型就加载一次编码器：模型文件变化后才重新构造。
        assert len(encoder.loads) == 2

    def test_multi_interest_backend_caches_runtime_model(self, monkeypatch, tmp_path):
        pytest.importorskip("torch")
        pd       = pytest.importorskip("pandas")
        np       = pytest.importorskip("numpy")
        joblib   = pytest.importorskip("joblib")
        backend  = importlib.import_module("plugins.arxiv_filter.inference.multi_interest_backend")
        shared   = importlib.import_module("plugins.arxiv_filter.inference.shared")
        numerics = importlib.import_module("plugins.arxiv_filter.numerics")
        backend._MODEL_CACHE.clear()

        # 一个只有一个兴趣中心的真实多兴趣模型：分类器在两条样本上拟合。
        centers = np.array([[1.0, 0.0]], dtype=np.float32)
        centroid = np.array([1.0, 0.0], dtype=np.float32)
        features, _, _ = numerics.build_interest_features(
            np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32), centers, centroid, None
        )
        joblib.dump(
            {
                "encoder_name"    : "fake-encoder",
                "n_interests"     : 1,
                "embedding_dim"   : 2,
                "interest_centers": centers,
                "pos_centroid"    : centroid,
                "neg_centroid"    : None,
                "classifier"      : backend.LogisticRegression().fit(features, [1, 0]),
                "threshold"       : 0.5,
                "threshold_beta"  : 1.0,
                "cluster_keywords": ["pulsar"],
                "cluster_sizes"   : [1],
                "cluster_examples": [["Pulsar"]],
                "feature_names"   : numerics.interest_feature_names(1),
                "columns"         : {},
            },
            tmp_path / "artifacts.joblib",
        )
        artifact = tmp_path / "artifact.json"
        artifact.write_text('{"version": 1}', encoding="utf-8")
        encoder = install_fake_encoder(monkeypatch)

        params = shared.InferenceParams(
            model_path = str(tmp_path),
            threshold  = 0.5,
            batch_size = 32,
            max_len    = 64,
            input_mode = "title_only",
            model_type = "multi_interest",
        )
        data = pd.DataFrame([{"Title": "Paper"}])

        backend.run_multi_interest_inference(params, data)
        backend.run_multi_interest_inference(params, data)
        artifact.write_text('{"version": 2}', encoding="utf-8")
        backend.run_multi_interest_inference(params, data)

        # 每构造一次模型就加载一次编码器：模型文件变化后才重新构造。
        assert len(encoder.loads) == 2
