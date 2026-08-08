"""
Pydantic Models 单元测试
"""

import pytest
from pydantic import ValidationError

from core.models import (
    OneBotEvent,
    PluginCommandManifest,
    PluginDependencyManifest,
    PluginManifest,
    PluginScheduleManifest,
    PluginServiceManifest,
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

    def test_message_from_json_array_text(self):
        """测试普通 JSON 数组文本不会被误判为消息段"""
        event = OneBotEvent(message="[1, 2]")
        assert isinstance(event.message, list)
        assert event.message == [{"type": "text", "data": {"text": "[1, 2]"}}]

    def test_message_from_empty_string(self):
        """测试空字符串消息"""
        event = OneBotEvent(message="")
        assert event.message == ""

    def test_raw_message_fills_missing_or_empty_message(self):
        """raw_message-only events use the standard text-segment contract."""
        for message in (None, "", []):
            event = OneBotEvent(message=message, raw_message="/help")
            assert event.message == [{"type": "text", "data": {"text": "/help"}}]

    def test_raw_message_does_not_replace_nonempty_segment_payload(self):
        message = [{"type": "image", "data": {"file": "image.png"}}]
        event = OneBotEvent(message=message, raw_message="[CQ:image,file=image.png]")
        assert event.message == message

    def test_message_from_invalid_json_string(self):
        """测试无效 JSON 字符串消息"""
        event = OneBotEvent(message="not json")
        assert isinstance(event.message, list)
        assert event.message[0]["type"] == "text"
        assert event.message[0]["data"]["text"] == "not json"

    @pytest.mark.parametrize(
        "message",
        [
            [{"type": "text", "data": "truthy"}],
            [{"type": "text", "data": None}],
            [{"type": "", "data": {}}],
            [{"data": {"text": "missing type"}}],
            ["not a segment"],
            123,
        ],
    )
    def test_rejects_malformed_structured_message(self, message):
        with pytest.raises(ValidationError):
            OneBotEvent(message=message)

    def test_rejects_malformed_json_segment_array(self):
        with pytest.raises(ValidationError):
            OneBotEvent(message='[{"type":"text","data":"truthy"}]')

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
        assert manifest.usage is None

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
        assert manifest.enabled is True

    def test_schedule_rejects_unknown_fields(self):
        with pytest.raises(ValidationError):
            PluginScheduleManifest(
                handler="job",
                cron={"hour": "*"},
                dangerous_unimplemented_option=True,
            )

    def test_schedule_preserves_explicit_empty_groups_and_rejects_invalid_ids(self):
        assert (
            PluginScheduleManifest(
                handler="job",
                cron={"hour": "*"},
                group_ids=[],
            ).group_ids
            == []
        )
        for group_ids in ([0], [-1], [True], [123, 123]):
            with pytest.raises(ValidationError):
                PluginScheduleManifest(
                    handler="job",
                    cron={"hour": "*"},
                    group_ids=group_ids,
                )


class TestPluginDependencyManifest:
    def test_dependency_defaults_to_required(self):
        dependency = PluginDependencyManifest(name="aiohttp")

        assert dependency.required is True
        assert dependency.description is None


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
        assert manifest.schema_version == 1
        assert manifest.dependencies == []
        assert manifest.services == []
        assert manifest.uses_services == []
        assert manifest.capabilities == []

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

    def test_manifest_rejects_unknown_runtime_fields(self):
        with pytest.raises(ValidationError):
            PluginManifest(name="test", pretend_concurrency_limit=1)

    def test_manifest_entry_uses_the_shared_strict_path_contract(self):
        assert PluginManifest(name="test", entry="nested/entry.py").entry == "nested/entry.py"
        for invalid in (
            "../main.py",
            "/main.py",
            "C:/main.py",
            "nested\\main.py",
            "nested//main.py",
            "sub.main.py",
            "__init__.py",
        ):
            with pytest.raises(ValidationError):
                PluginManifest(name="test", entry=invalid)

    def test_manifest_watch_files_are_bounded_canonical_and_unique(self):
        manifest = PluginManifest(
            name="test",
            watch_files=["config/settings.json", "assets/catalog.json"],
        )
        assert manifest.watch_files == ["config/settings.json", "assets/catalog.json"]

        for invalid in (
            ["data/state.json"],
            ["config/settings.txt"],
            ["config/settings.json", "config/settings.json"],
            [f"config/{index}.json" for index in range(65)],
        ):
            with pytest.raises(ValidationError):
                PluginManifest(name="test", watch_files=invalid)

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

    def test_command_catalog_rejects_duplicate_root_codes(self):
        command = {"name": "same", "triggers": ["one"], "help": "same"}
        with pytest.raises(ValidationError, match="duplicate stable names"):
            PluginManifest.model_validate(
                {
                    "name": "test",
                    "commands": [command, {**command, "triggers": ["two"]}],
                }
            )

    def test_command_catalog_rejects_excessive_depth(self):
        child = {"name": "level9", "help": "level 9", "usage": "/deep"}
        for depth in range(8, 1, -1):
            child = {
                "name": f"level{depth}",
                "help": f"level {depth}",
                "usage": "/deep",
                "subcommands": [child],
            }
        with pytest.raises(ValidationError, match="8 levels"):
            PluginManifest.model_validate(
                {
                    "name": "test",
                    "commands": [
                        {
                            "name": "deep",
                            "triggers": ["deep"],
                            "help": "deep",
                            "subcommands": [child],
                        }
                    ],
                }
            )

    def test_command_catalog_rejects_more_than_512_nodes(self):
        commands = []
        for root_index in range(5):
            commands.append(
                {
                    "name": f"root{root_index}",
                    "triggers": [f"root{root_index}"],
                    "help": "root",
                    "subcommands": [
                        {
                            "name": f"child{child_index}",
                            "help": "child",
                            "usage": "/root child",
                        }
                        for child_index in range(128)
                    ],
                }
            )
        with pytest.raises(ValidationError, match="512 nodes"):
            PluginManifest.model_validate({"name": "test", "commands": commands})

    def test_service_contracts_are_closed_and_provider_scoped(self):
        service = PluginServiceManifest(
            name="voice.synthesize_text",
            callback="convert_text_to_voice",
            callers=["smalltalk"],
        )
        manifest = PluginManifest(name="voice", services=[service])
        assert manifest.services == [service]

        invalid_services = [
            {
                "name": "voice.synthesize_text",
                "callback": "_private",
                "callers": ["smalltalk"],
            },
            {
                "name": "voice.synthesize_text",
                "callback": "convert_text_to_voice",
                "callers": ["shell"],
            },
            {
                "name": "shell.handle",
                "callback": "handle",
                "callers": ["smalltalk"],
            },
        ]
        for raw_service in invalid_services:
            with pytest.raises(ValidationError):
                PluginManifest(name="voice", services=[raw_service])

        with pytest.raises(ValidationError):
            PluginManifest(
                name="voice",
                services=[service.model_dump(), service.model_dump()],
            )

        with pytest.raises(ValidationError):
            PluginManifest(name="chat", services=[service])

    def test_codex_service_requires_exact_capability_and_caller(self):
        valid = {
            "name": "codex.enqueue_arxiv_summary",
            "callback": "enqueue_arxiv_summary_service",
            "callers": ["arxiv_filter"],
            "required_capability": "codex_arxiv_summary",
        }
        assert PluginManifest(name="codex", services=[valid]).services
        for changed in (
            {**valid, "required_capability": None},
            {**valid, "callers": ["smalltalk"]},
        ):
            with pytest.raises(ValidationError):
                PluginManifest(name="codex", services=[changed])

    def test_capabilities_are_manifest_declared_but_core_owner_scoped(self):
        assert PluginManifest(
            name="bot_core",
            capabilities=["secret_admin"],
        ).capabilities == ["secret_admin"]
        assert PluginManifest(
            name="codex",
            capabilities=["admin_sessions", "execution_timeout_exempt"],
        ).capabilities == ["admin_sessions", "execution_timeout_exempt"]

        for payload in (
            {"name": "other", "capabilities": ["secret_admin"]},
            {"name": "qingssh", "capabilities": ["secret_admin"]},
            {
                "name": "bot_core",
                "capabilities": ["secret_admin", "secret_admin"],
            },
        ):
            with pytest.raises(ValidationError):
                PluginManifest.model_validate(payload)

    def test_service_consumers_and_core_observer_are_closed_contracts(self):
        smalltalk = PluginManifest(
            name="smalltalk",
            uses_services=["chat.reply", "voice.synthesize_text"],
        )
        assert smalltalk.uses_services == ["chat.reply", "voice.synthesize_text"]
        observer = {
            "name": "core.observe_outgoing_action",
            "callback": "observe_outgoing_action",
            "callers": ["core"],
        }
        assert PluginManifest(name="xiaoqing_chat", services=[observer]).services

        for payload in (
            {"name": "shell", "uses_services": ["chat.reply"]},
            {
                "name": "smalltalk",
                "uses_services": ["chat.reply", "chat.reply"],
            },
            {"name": "other", "services": [observer]},
        ):
            with pytest.raises(ValidationError):
                PluginManifest.model_validate(payload)
