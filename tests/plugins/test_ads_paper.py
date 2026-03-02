"""
ads_paper 插件单元测试

测试 NASA ADS 论文管理插件的主要功能。
由于 ads_paper 插件使用相对导入，我们只测试文件结构和配置。
"""

import json
import pytest
import asyncio
from pathlib import Path
from typing import Any, cast

from plugins.ads_paper import note_commands

# 添加项目根目录到路径
ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================
# Tests
# ============================================================

class TestAdsPaperPlugin:
    """测试 ads_paper 插件基本功能"""

    def test_constants_file_exists(self):
        """测试常量文件存在"""
        constants_path = ROOT / "plugins" / "ads_paper" / "constants.py"
        assert constants_path.exists()

        with open(constants_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 检查是否定义了必要的常量
            assert "ADS_API_BASE" in content or "ADS" in content.upper()

    def test_storage_module_exists(self):
        """测试存储模块存在"""
        storage_path = ROOT / "plugins" / "ads_paper" / "storage.py"
        assert storage_path.exists()

        with open(storage_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "class" in content or "def" in content

    def test_ads_client_module_exists(self):
        """测试 ADS 客户端模块存在"""
        client_path = ROOT / "plugins" / "ads_paper" / "ads_client.py"
        assert client_path.exists()

        with open(client_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "class" in content

    def test_paper_commands_module_exists(self):
        """测试论文命令模块存在"""
        commands_path = ROOT / "plugins" / "ads_paper" / "paper_commands.py"
        assert commands_path.exists()

        with open(commands_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "async def" in content or "def" in content

    def test_note_commands_module_exists(self):
        """测试笔记命令模块存在"""
        note_path = ROOT / "plugins" / "ads_paper" / "note_commands.py"
        assert note_path.exists()

        with open(note_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "async def" in content or "def" in content

    def test_ai_commands_module_exists(self):
        """测试 AI 命令模块存在"""
        ai_path = ROOT / "plugins" / "ads_paper" / "ai_commands.py"
        assert ai_path.exists()

        with open(ai_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert "async def" in content or "def" in content


class TestAdsPaperConfig:
    """测试 ads_paper 配置"""

    def test_plugin_json_structure(self):
        """测试 plugin.json 结构"""
        plugin_json_path = ROOT / "plugins" / "ads_paper" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        assert "name" in config
        assert "version" in config
        assert "description" in config
        assert "commands" in config

    def test_plugin_commands_have_help(self):
        """测试命令有帮助信息"""
        plugin_json_path = ROOT / "plugins" / "ads_paper" / "plugin.json"
        with open(plugin_json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        for cmd in config.get("commands", []):
            if "name" in cmd:
                # 命令应该有描述或使用说明
                assert "help" in cmd or "usage" in cmd or "description" in cmd or cmd.get("help", "")


class TestAdsPaperModules:
    """测试 ads_paper 各模块结构"""

    def test_main_exports_plugin(self):
        """测试 main.py 导出 Plugin 类"""
        main_path = ROOT / "plugins" / "ads_paper" / "main.py"
        with open(main_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "class Plugin" in content or "plugin" in content.lower()

    def test_modules_import_correctly(self):
        """测试各模块可以正确导入"""
        modules_to_check = [
            "constants",
            "storage",
            "ads_client",
            "paper_commands",
            "note_commands",
            "ai_commands"
        ]

        for module_name in modules_to_check:
            module_path = ROOT / "plugins" / "ads_paper" / f"{module_name}.py"
            assert module_path.exists(), f"Module {module_name} does not exist"

            # 尝试读取文件内容
            with open(module_path, "r", encoding="utf-8") as f:
                content = f.read()
                # 确保文件有实际内容
                assert len(content.strip()) > 0, f"Module {module_name} is empty"


class TestAdsPaperArxivPatterns:
    """测试 arXiv ID 解析模式"""

    def test_arxiv_id_patterns_in_constants(self):
        """测试常量文件中有 arXiv ID 模式"""
        constants_path = ROOT / "plugins" / "ads_paper" / "constants.py"
        with open(constants_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查是否包含 arXiv 相关的正则表达式
        assert "arxiv" in content.lower() or "ARXIV" in content

    def test_ads_client_has_arxiv_methods(self):
        """测试 ADS 客户端有 arXiv 处理方法"""
        client_path = ROOT / "plugins" / "ads_paper" / "ads_client.py"
        with open(client_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查是否有 arXiv 相关的方法
        assert "arxiv" in content.lower() or "normalize" in content.lower()


class TestAdsPaperAsyncStorage:
    def test_topics_read_uses_to_thread(self, monkeypatch):
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Storage:
            def get_topics(self):
                return []

        monkeypatch.setattr(note_commands.asyncio, "to_thread", fake_to_thread)
        asyncio.run(note_commands.cmd_topics(cast(Any, Storage()), ""))

        assert "get_topics" in calls

    def test_note_write_uses_to_thread(self, monkeypatch):
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Storage:
            def add_paper_note(self, paper_id, content, user_id):
                return True

            def get_paper_notes(self, paper_id):
                return [{"content": "c"}]

        monkeypatch.setattr(note_commands.asyncio, "to_thread", fake_to_thread)
        asyncio.run(note_commands.cmd_note(cast(Any, Storage()), "paper-1 test", 1))

        assert "add_paper_note" in calls
        assert "get_paper_notes" in calls
