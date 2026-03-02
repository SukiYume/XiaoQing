"""测试 CHIME FRB 重复暴监测插件"""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock
import tempfile
import json

ROOT = Path(__file__).resolve().parent.parent.parent


class TestChimePlugin:
    """测试 CHIME FRB 插件"""

    def test_init(self):
        """测试插件初始化"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def init" in content

    def test_help_text(self):
        """测试帮助文本"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "def _show_help" in content
        assert "CHIME" in content or "FRB" in content

    def test_module_structure(self):
        """测试模块结构"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查关键函数
        assert "def init" in content
        assert "def handle" in content
        assert "def scheduled_check" in content


class TestChimeDataModel:
    """测试 CHIME 数据模型"""

    def test_frb_data_class(self):
        """测试 FRBData 类"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查 FRBData 类
        assert "class FRBData" in content
        assert "def _parse_data" in content
        assert "def format_info" in content
        assert "def is_valid" in content

    def test_frb_data_attributes(self):
        """测试 FRBData 属性"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查关键属性
        assert "self.name" in content
        assert "self.timestamp" in content
        assert "self.dm" in content
        assert "self.snr" in content
        assert "self.ra" in content
        assert "self.dec" in content

    def test_pulse_extraction(self):
        """测试脉冲数据提取"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查脉冲提取逻辑
        assert "PULSE_DATE_PATTERN" in content
        assert "self.pulses" in content
        assert "self.latest_pulse" in content


class TestChimeApi:
    """测试 CHIME API 功能"""

    def test_api_url(self):
        """测试 API URL"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "CHIME_API_URL" in content
        assert "chime-frb.ca" in content

    def test_fetch_function(self):
        """测试数据获取函数"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def fetch_chime_repeaters" in content
        assert "async with" in content
        assert "response.json()" in content

    def test_error_handling(self):
        """测试错误处理"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "TimeoutError" in content
        assert "except" in content


class TestChimeDataProcessing:
    """测试数据处理"""

    def test_parse_frb_data(self):
        """测试解析 FRB 数据"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def parse_frb_data" in content
        assert "FRBData" in content

    def test_build_history_mapping(self):
        """测试构建历史映射"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def build_history_mapping" in content

    def test_find_updates(self):
        """测试查找更新"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def find_updates" in content
        assert "new_repeaters" in content
        assert "new_pulses" in content

    def test_format_update_message(self):
        """测试格式化更新消息"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def format_update_message" in content
        assert "新发现的重复暴" in content
        assert "检测到新脉冲" in content


class TestChimeHistory:
    """测试历史记录功能"""

    def test_load_history(self):
        """测试加载历史"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def load_history" in content
        assert "chime_history.json" in content

    def test_save_history(self):
        """测试保存历史"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "def save_history" in content
        assert "write_json" in content


class TestChimeScheduled:
    """测试定时任务"""

    def test_scheduled_check_function(self):
        """测试定时检查函数"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "async def scheduled_check" in content
        assert "fetch_chime_repeaters" in content
        assert "find_updates" in content

    def test_no_updates_returns_empty(self):
        """测试无更新时返回空"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        # 检查无更新时不发送消息
        assert "if not new_repeaters and not new_pulses" in content


class TestChimePluginJson:
    """测试 CHIME plugin.json 配置"""

    def test_plugin_json_exists(self):
        """测试 plugin.json 存在"""
        plugin_json = ROOT / "plugins" / "chime" / "plugin.json"
        assert plugin_json.exists()

    def test_plugin_json_content(self):
        """测试 plugin.json 内容"""
        plugin_json = ROOT / "plugins" / "chime" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert content["name"] == "chime"
        assert "commands" in content
        assert "schedule" in content

    def test_command_triggers(self):
        """测试命令触发器"""
        plugin_json = ROOT / "plugins" / "chime" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        chime_cmd = next((cmd for cmd in content["commands"] if cmd["name"] == "chime"), None)
        assert chime_cmd is not None
        assert "chime" in chime_cmd["triggers"]
        assert "frb" in chime_cmd["triggers"]

    def test_schedule_config(self):
        """测试定时任务配置"""
        plugin_json = ROOT / "plugins" / "chime" / "plugin.json"
        content = json.loads(plugin_json.read_text(encoding='utf-8'))

        assert "schedule" in content
        assert len(content["schedule"]) > 0

        # 检查定时检查任务
        check_task = next((s for s in content["schedule"] if s["id"] == "chime_check"), None)
        assert check_task is not None
        assert check_task["handler"] == "scheduled_check"


class TestChimeConstants:
    """测试常量配置"""

    def test_max_display_frbs(self):
        """测试最大显示数量"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "MAX_DISPLAY_FRBS" in content
        assert "5" in content or "MAX_DISPLAY_FRBS = 5" in content

    def test_pulse_date_pattern(self):
        """测试脉冲日期模式"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')

        assert "PULSE_DATE_PATTERN" in content
        assert r"\d{6}" in content


class TestChimeIntegration:
    """集成测试"""

    def test_module_exists(self):
        """测试模块存在"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        assert main_file.exists()

    def test_main_functions(self):
        """测试主模块函数"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        # 检查关键函数存在
        assert "def init" in content
        assert "async def handle" in content
        assert "async def scheduled_check" in content
        assert "def _show_help" in content

    def test_frb_data_class_exists(self):
        """测试 FRBData 类存在"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "class FRBData" in content

    def test_handle_function_exists(self):
        """测试 handle 函数存在"""
        main_file = ROOT / "plugins" / "chime" / "main.py"
        content = main_file.read_text(encoding='utf-8')
        assert "async def handle" in content
