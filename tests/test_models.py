"""
Pydantic Models 单元测试
"""

import json
import pytest
from typing import Any

from core.models import (
    OneBotEvent,
    PluginCommandManifest,
    PluginScheduleManifest,
    PluginManifest,
)

# ============================================================
# OneBotEvent 测试
# ============================================================

class TestOneBotEvent:
    """OneBotEvent 模型测试"""

    def test_create_minimal_event(self):
        """测试创建最小事件"""
        event = OneBotEvent()
        assert event.time is None
        assert event.self_id is None
        assert event.post_type is None

    def test_create_full_event(self):
        """测试创建完整事件"""
        event = OneBotEvent(
            time=1234567890,
            self_id=11111,
            post_type="message",
            message_type="group",
            user_id=12345,
            group_id=67890,
            message=[{"type": "text", "data": {"text": "hello"}}],
            raw_message="hello",
        )
        assert event.time == 1234567890
        assert event.self_id == 11111
        assert event.post_type == "message"
        assert event.message_type == "group"
        assert event.user_id == 12345
        assert event.group_id == 67890

    def test_message_from_string(self):
        """测试从字符串创建消息"""
        event = OneBotEvent(message="hello world")
        # 字符串应该被转换为消息段
        assert isinstance(event.message, list)
        assert event.message[0]["type"] == "text"
        assert event.message[0]["data"]["text"] == "hello world"

    def test_message_from_list(self):
        """测试从列表创建消息"""
        segments = [
            {"type": "text", "data": {"text": "hello "}},
            {"type": "image", "data": {"file": "test.png"}},
        ]
        event = OneBotEvent(message=segments)
        assert event.message == segments

    def test_message_from_json_string(self):
        """测试从 JSON 字符串创建消息"""
        json_str = '[{"type": "text", "data": {"text": "hello"}}]'
        event = OneBotEvent(message=json_str)
        assert isinstance(event.message, list)
        assert event.message[0]["type"] == "text"

    def test_message_from_empty_string(self):
        """测试空字符串消息"""
        event = OneBotEvent(message="")
        assert event.message == ""

    def test_message_from_invalid_json_string(self):
        """测试无效 JSON 字符串消息"""
        event = OneBotEvent(message="not json")
        assert isinstance(event.message, list)
        assert event.message[0]["type"] == "text"
        assert event.message[0]["data"]["text"] == "not json"

    def test_model_validate_dict(self):
        """测试从字典验证"""
        data = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 67890,
            "message": "test",
        }
        event = OneBotEvent.model_validate(data)
        assert event.post_type == "message"
        assert event.user_id == 12345

    def test_extra_fields_allowed(self):
        """测试允许额外字段"""
        data = {
            "post_type": "message",
            "custom_field": "custom_value",
            "another_field": 123,
        }
        event = OneBotEvent.model_validate(data)
        assert event.post_type == "message"
        # extra="allow" 模式下额外字段被保留
        assert event.model_dump()["custom_field"] == "custom_value"

# ============================================================
# PluginCommandManifest 测试
# ============================================================

class TestPluginCommandManifest:
    """PluginCommandManifest 测试"""

    def test_create_command_manifest(self):
        """测试创建命令清单"""
        manifest = PluginCommandManifest(
            name="echo",
            triggers=["echo", "回显"],
            help="回显消息",
            admin_only=False,
            priority=0,
        )
        assert manifest.name == "echo"
        assert manifest.triggers == ["echo", "回显"]
        assert manifest.help == "回显消息"
        assert manifest.admin_only is False
        assert manifest.priority == 0

    def test_default_values(self):
        """测试默认值"""
        manifest = PluginCommandManifest(
            name="test",
            triggers=["test"],
            help="test command",
        )
        assert manifest.admin_only is False
        assert manifest.priority == 0

# ============================================================
# PluginScheduleManifest 测试
# ============================================================

class TestPluginScheduleManifest:
    """PluginScheduleManifest 测试"""

    def test_create_schedule_manifest(self):
        """测试创建定时任务清单"""
        manifest = PluginScheduleManifest(
            handler="daily_job",
            cron={"hour": "9", "minute": "0"},
            id="daily_9am",
            group_ids=[123, 456],
        )
        assert manifest.handler == "daily_job"
        assert manifest.cron == {"hour": "9", "minute": "0"}
        assert manifest.id == "daily_9am"
        assert manifest.group_ids == [123, 456]

    def test_default_values(self):
        """测试默认值"""
        manifest = PluginScheduleManifest(
            handler="job",
            cron={"hour": "*"},
        )
        assert manifest.id is None
        assert manifest.group_ids is None

# ============================================================
# PluginManifest 测试
# ============================================================

class TestPluginManifest:
    """PluginManifest 测试"""

    def test_create_minimal_manifest(self):
        """测试创建最小清单"""
        manifest = PluginManifest(name="test_plugin")
        assert manifest.name == "test_plugin"
        assert manifest.version == "0.0.0"
        assert manifest.entry == "main.py"
        assert manifest.commands == []
        assert manifest.schedule == []
        assert manifest.concurrency == "parallel"
        assert manifest.enabled is True

    def test_create_full_manifest(self):
        """测试创建完整清单"""
        manifest = PluginManifest(
            name="my_plugin",
            version="1.0.0",
            entry="custom.py",
            commands=[
                PluginCommandManifest(
                    name="cmd1",
                    triggers=["cmd1"],
                    help="Command 1",
                ),
            ],
            schedule=[
                PluginScheduleManifest(
                    handler="job1",
                    cron={"hour": "*"},
                ),
            ],
            concurrency="sequential",
            enabled=True,
        )
        assert manifest.name == "my_plugin"
        assert manifest.version == "1.0.0"
        assert manifest.entry == "custom.py"
        assert len(manifest.commands) == 1
        assert len(manifest.schedule) == 1
        assert manifest.concurrency == "sequential"

    def test_model_validate_from_dict(self):
        """测试从字典验证"""
        data = {
            "name": "test",
            "version": "2.0.0",
            "commands": [
                {
                    "name": "hello",
                    "triggers": ["hello"],
                    "help": "Say hello",
                }
            ],
        }
        manifest = PluginManifest.model_validate(data)
        assert manifest.name == "test"
        assert manifest.version == "2.0.0"
        assert len(manifest.commands) == 1
        assert manifest.commands[0].name == "hello"

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
