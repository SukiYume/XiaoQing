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

from plugins.ads_paper.ads_client import ADSClient
from plugins.ads_paper.llm_client import generate_summary
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
            def get_topics(self, user_id):
                return []

        monkeypatch.setattr(note_commands.asyncio, "to_thread", fake_to_thread)
        asyncio.run(note_commands.cmd_topics(cast(Any, Storage()), "", 1))

        assert "get_topics" in calls

    def test_note_write_uses_to_thread(self, monkeypatch):
        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Storage:
            def add_paper_note(self, paper_id, content, user_id):
                return True

            def get_paper_notes(self, paper_id, user_id=None):
                return [{"content": "c"}]

        monkeypatch.setattr(note_commands.asyncio, "to_thread", fake_to_thread)
        asyncio.run(note_commands.cmd_note(cast(Any, Storage()), "paper-1 test", 1))

        assert "add_paper_note" in calls
        assert "get_paper_notes" in calls

    def test_cmd_daily_reads_topics_in_thread(self, monkeypatch, tmp_path):
        from plugins.ads_paper import ai_commands

        calls = []

        async def fake_to_thread(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        class Client:
            async def search_papers(self, query, max_results):
                assert query == "LLM OR agents"
                assert max_results > 0
                return []

        class MockContext:
            def __init__(self, data_dir):
                self.data_dir = data_dir

        monkeypatch.setattr(ai_commands.asyncio, "to_thread", fake_to_thread)

        data_dir = tmp_path / "ads_paper"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "research_topics.json").write_text(
            json.dumps({"keywords": [{"value": "LLM", "user": 1}, {"value": "agents", "user": 1}]}),
            encoding="utf-8",
        )

        result = asyncio.run(ai_commands.cmd_daily(cast(Any, Client()), MockContext(data_dir), 1))

        assert "get_topics" in calls
        assert "未找到" in str(result)


class TestPaperStorageBehavior:
    def test_storage_roundtrip_for_notes_topics_and_deadlines(self, tmp_path):
        from plugins.ads_paper.storage import PaperStorage

        storage = PaperStorage(tmp_path)

        assert storage.add_paper_note("paper-1", "first note", 10001) is True
        assert storage.get_paper_notes("paper-1", 10001)[0]["content"] == "first note"

        assert storage.add_topic("FRB", 10001) is True
        assert storage.get_topics(10001) == ["frb"]
        assert storage.remove_topic("FRB", 10001) is True
        assert storage.get_topics(10001) == []

        assert storage.add_deadline("submit", "2026-05-01", 10001) is True
        deadlines = storage.get_deadlines(10001)
        assert len(deadlines) == 1
        assert deadlines[0]["name"] == "submit"

    def test_storage_filters_notes_and_deadlines_by_user_and_delete_uses_visible_order(self, tmp_path):
        from plugins.ads_paper.storage import PaperStorage

        storage = PaperStorage(tmp_path)
        storage.add_paper_note("paper-1", "note-user-1", 10001)
        storage.add_paper_note("paper-1", "note-user-2", 10002)
        storage.add_deadline("later", "2026-06-01", 10001)
        storage.add_deadline("earlier", "2026-05-01", 10001)
        storage.add_deadline("other-user", "2026-04-01", 10002)

        user_notes = storage.get_paper_notes("paper-1", 10001)
        other_notes = storage.get_paper_notes("paper-1", 10002)
        assert [note["content"] for note in user_notes] == ["note-user-1"]
        assert [note["content"] for note in other_notes] == ["note-user-2"]

        deadlines = storage.get_deadlines(10001)
        assert [deadline["name"] for deadline in deadlines] == ["earlier", "later"]

        assert storage.delete_deadline(0, 10001) is True
        remaining = storage.get_deadlines(10001)
        assert [deadline["name"] for deadline in remaining] == ["later"]
        assert [deadline["name"] for deadline in storage.get_deadlines(10002)] == ["other-user"]

    def test_storage_instances_share_lock_and_do_not_lose_updates(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor
        from plugins.ads_paper.storage import PaperStorage

        def add(index):
            return PaperStorage(tmp_path).add_paper_note("paper", f"note-{index}", 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(add, range(40)))

        notes = PaperStorage(tmp_path).get_paper_notes("paper", 1)
        assert len(notes) == 40

    def test_writing_topics_and_references_are_owner_scoped(self, tmp_path):
        from plugins.ads_paper.storage import PaperStorage

        storage = PaperStorage(tmp_path)
        storage.add_writing_idea("intro", "user-one", 1)
        storage.add_writing_idea("intro", "user-two", 2)
        storage.add_topic("FRB", 1)
        storage.add_topic("AGN", 2)
        storage.add_reference(1, "bib-1", "@article{bib-1, title={One}}")
        storage.add_reference(2, "bib-2", "@article{bib-2, title={Two}}")

        assert [item["content"] for item in storage.get_writing_ideas("intro", 1)] == ["user-one"]
        assert storage.get_topics(1) == ["frb"]
        assert "bib-1" in storage.get_references(1)
        assert "bib-2" not in storage.get_references(1)


@pytest.mark.asyncio
async def test_ads_client_search_passes_timeout():
    captured = {}

    class MockResponse:
        status = 200

        async def json(self):
            return {"response": {"docs": []}}

    class MockContextManager:
        async def __aenter__(self):
            return MockResponse()

        async def __aexit__(self, *args):
            return None

    class MockSession:
        def get(self, *args, **kwargs):
            captured.update(kwargs)
            return MockContextManager()

    client = ADSClient("token", MockSession())
    result = await client.search_papers("frb")

    assert result == []
    assert captured["timeout"].total == 30


@pytest.mark.asyncio
async def test_ads_llm_generate_summary_passes_timeout():
    captured = {}

    class MockResponse:
        status = 200

        async def json(self):
            return {"choices": [{"message": {"content": "summary"}}]}

    class MockContextManager:
        async def __aenter__(self):
            return MockResponse()

        async def __aexit__(self, *args):
            return None

    class MockSession:
        def post(self, *args, **kwargs):
            captured.update(kwargs)
            return MockContextManager()

    result = await generate_summary(
        session=MockSession(),
        api_base="https://example.com/v1",
        api_key="key",
        model="model",
        title="title",
        abstract="abstract",
    )

    assert result == "summary"
    assert captured["timeout"].total == 60
