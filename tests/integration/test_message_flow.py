"""End-to-end message flow tests"""

import asyncio
import json
import pytest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from core.dispatcher import Dispatcher
from core.router import CommandRouter, CommandSpec
from core.session import SessionManager
from core.plugin_manager import PluginManager


class TestMessageFlow:
    """Test complete message processing flow"""

    @pytest.fixture
    def test_dispatcher(self, temp_dir: Path):
        """Create a test dispatcher with minimal setup"""
        from core.config import ConfigManager

        config_file = temp_dir / "config.json"
        secrets_file = temp_dir / "secrets.json"

        # Write minimal config
        with open(config_file, "w") as f:
            json.dump({
                "bot_name": "TestBot",
                "command_prefixes": ["/"],
                "require_bot_name_in_group": False,
            }, f)
        with open(secrets_file, "w") as f:
            json.dump({"admin_user_ids": [12345]}, f)

        config_manager = ConfigManager(config_file, secrets_file)
        session_manager = SessionManager()
        router = CommandRouter()

        # Mock components
        mock_registry = MagicMock()
        mock_registry.get = Mock(return_value=None)

        mock_admin_check = MagicMock()
        mock_admin_check.is_admin = Mock(return_value=True)

        mock_context = MagicMock()
        mock_context_factory = Mock(return_value=mock_context)

        # Create a simple handler
        async def echo_handler(name, args, event, context):
            from core.plugin_base import segments
            return segments(args or "")

        # Register echo command
        router.register(CommandSpec(
            plugin="echo",
            name="echo",
            triggers=["echo", "回显"],
            help_text="Echo message",
            admin_only=False,
            handler=echo_handler,
            priority=0,
        ))

        dispatcher = Dispatcher(
            router=router,
            config_provider=config_manager,
            plugin_registry=mock_registry,
            admin_check=mock_admin_check,
            build_context=mock_context_factory,
            semaphore=asyncio.Semaphore(10),
            session_manager=session_manager,
        )

        return dispatcher, session_manager

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_simple_command_flow(self, test_dispatcher):
        """Test simple command flow: message receive -> route -> handle -> response"""
        dispatcher, _ = test_dispatcher

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "message": [{"type": "text", "data": {"text": "/echo hello"}}],
            "raw_message": "/echo hello",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) > 0
        assert "hello" in str(responses[0])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_command_handling(self, test_dispatcher):
        """Test unknown command handling"""
        dispatcher, _ = test_dispatcher

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "message": [{"type": "text", "data": {"text": "/unknown_cmd"}}],
            "raw_message": "/unknown_cmd",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        # Unknown command with prefix returns error message
        assert responses is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_group_message_with_bot_mention(self, test_dispatcher):
        """Test group message with @bot"""
        dispatcher, _ = test_dispatcher

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 50001,
            "self_id": 11111,
            "message": [
                {"type": "at", "data": {"qq": "11111"}},
                {"type": "text", "data": {"text": " echo hello"}}
            ],
            "raw_message": "[@11111] echo hello",
        }

        responses = await dispatcher.handle_event(event)
        # Should handle because of @ mention
        assert responses is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ignore_self_message(self, test_dispatcher):
        """Test that messages from self are ignored"""
        dispatcher, _ = test_dispatcher

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 11111,  # Same as self_id
            "group_id": 50001,
            "self_id": 11111,
            "message": [{"type": "text", "data": {"text": "hello"}}],
            "raw_message": "hello",
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_non_message_event_ignored(self, test_dispatcher):
        """Test that non-message events are ignored"""
        dispatcher, _ = test_dispatcher

        event = {
            "post_type": "notice",
            "notice_type": "group_increase",
            "user_id": 12345,
            "group_id": 50001,
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_private_message_always_processed(self, test_dispatcher):
        """Test that private messages are always processed"""
        dispatcher, _ = test_dispatcher

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "message": [{"type": "text", "data": {"text": "/echo test"}}],
            "raw_message": "/echo test",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) > 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_dispatcher_with_session(self, test_dispatcher):
        """Test dispatcher interaction with session manager"""
        dispatcher, session_manager = test_dispatcher

        # Create an active session
        await session_manager.create(
            user_id=12345,
            group_id=50001,
            plugin_name="test",
            initial_data={"step": 1},
        )

        # Verify session exists
        assert await session_manager.exists(12345, 50001)

        # Message from user with active session should be processed
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 50001,
            "message": [{"type": "text", "data": {"text": "continue"}}],
            "raw_message": "continue",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        # Session exists but plugin doesn't handle it - returns empty or exit message
        assert responses is not None


class TestDispatcherIntegration:
    """Test dispatcher integration with various components"""

    @pytest.fixture
    def dispatcher_with_router(self, sample_router: CommandRouter, temp_dir: Path):
        """Create a dispatcher with router"""
        from core.config import ConfigManager

        config_file = temp_dir / "config.json"
        secrets_file = temp_dir / "secrets.json"

        # Write minimal config
        with open(config_file, "w") as f:
            json.dump({"bot_name": "TestBot", "command_prefixes": ["/"]}, f)
        with open(secrets_file, "w") as f:
            json.dump({"admin_user_ids": [12345]}, f)

        config_manager = ConfigManager(config_file, secrets_file)
        session_manager = SessionManager()

        # Mock components
        mock_registry = MagicMock()
        mock_registry.get = Mock(return_value=None)

        mock_admin_check = MagicMock()
        mock_admin_check.is_admin = Mock(return_value=True)

        mock_context = MagicMock()
        mock_context_factory = Mock(return_value=mock_context)

        dispatcher = Dispatcher(
            router=sample_router,
            config_provider=config_manager,
            plugin_registry=mock_registry,
            admin_check=mock_admin_check,
            build_context=mock_context_factory,
            semaphore=asyncio.Semaphore(10),
            session_manager=session_manager,
        )

        return dispatcher, session_manager

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_command_resolution_and_execution(self, dispatcher_with_router):
        """Test that commands are resolved and executed correctly"""
        dispatcher, _ = dispatcher_with_router

        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 12345,
            "message": [{"type": "text", "data": {"text": "/echo test message"}}],
            "raw_message": "/echo test message",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) > 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_group_message_requirement(self, dispatcher_with_router):
        """Test group message processing based on require_bot_name_in_group"""
        dispatcher, _ = dispatcher_with_router

        # With require_bot_name_in_group=True, message without prefix/bot name should be ignored
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 50001,
            "message": [{"type": "text", "data": {"text": "random message"}}],
            "raw_message": "random message",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) == 0  # Should be ignored

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_message_with_prefix_always_processed(self, dispatcher_with_router):
        """Test that messages with command prefix are always processed"""
        dispatcher, _ = dispatcher_with_router

        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 50001,
            "message": [{"type": "text", "data": {"text": "/echo hello"}}],
            "raw_message": "/echo hello",
            "self_id": 11111,
        }

        responses = await dispatcher.handle_event(event)
        assert len(responses) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration"])
