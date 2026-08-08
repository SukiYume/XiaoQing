"""End-to-end message flow tests"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from core.dispatcher import Dispatcher, MessageContext
from core.plugin_execution import PluginExecutionGate
from core.router import CommandRouter, CommandSpec
from core.session import SessionManager


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
            json.dump(
                {
                    "bot_name": "TestBot",
                    "command_prefixes": ["/"],
                    "require_bot_name_in_group": False,
                },
                f,
            )
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
        router.register(
            CommandSpec(
                plugin="echo",
                name="echo",
                triggers=["echo", "回显"],
                help_text="Echo message",
                admin_only=False,
                handler=echo_handler,
                priority=0,
            )
        )

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
                {"type": "text", "data": {"text": " echo hello"}},
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

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_dispatcher_serializes_same_session_handler_transactions(self, test_dispatcher):
        """Two inbound events cannot lose a same-session update across an await."""
        dispatcher, session_manager = test_dispatcher
        await session_manager.create(
            user_id=12345,
            group_id=50001,
            plugin_name="stateful",
            initial_data={"counter": 0},
        )

        async def handle_session(_text, _event, _context, session):
            counter = session.get("counter", 0)
            await asyncio.sleep(0)
            session.set("counter", counter + 1)
            return []

        loaded = SimpleNamespace(module=SimpleNamespace(handle_session=handle_session))
        dispatcher.plugin_registry.get.side_effect = lambda name: (
            loaded if name == "stateful" else None
        )
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 50001,
            "message": [{"type": "text", "data": {"text": "continue"}}],
            "raw_message": "continue",
            "self_id": 11111,
        }

        await asyncio.gather(dispatcher.handle_event(event), dispatcher.handle_event(event))

        session = await session_manager.peek(12345, 50001)
        assert session is not None
        assert session.get("counter") == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_failed_continuation_rolls_back_before_closing_original_session(
        self,
        test_dispatcher,
    ):
        """The close hook and cleanup never observe a handler's partial writes."""

        dispatcher, session_manager = test_dispatcher
        original_data = {"step": "ready", "nested": {"items": ["kept"]}}
        await session_manager.create(
            user_id=12345,
            group_id=50001,
            plugin_name="stateful",
            initial_data=original_data,
        )
        close_snapshots: list[dict[str, object]] = []

        async def handle_session(_text, _event, _context, session):
            session.set("step", "partial")
            session.get("nested")["items"].append("must-rollback")
            raise RuntimeError("continuation failed")

        async def close_session(_event, _context, session):
            close_snapshots.append(
                {
                    "step": session.get("step"),
                    "items": list(session.get("nested")["items"]),
                }
            )

        loaded = SimpleNamespace(
            module=SimpleNamespace(
                handle_session=handle_session,
                close_session=close_session,
            ),
            execution_gate=None,
        )
        dispatcher.plugin_registry.get.side_effect = lambda name: (
            loaded if name == "stateful" else None
        )
        event = {
            "post_type": "message",
            "message_type": "group",
            "user_id": 12345,
            "group_id": 50001,
            "message": [{"type": "text", "data": {"text": "continue"}}],
            "raw_message": "continue",
            "self_id": 11111,
        }

        response = await dispatcher.handle_event(event)

        assert response
        assert close_snapshots == [{"step": "ready", "items": ["kept"]}]
        assert await session_manager.peek(12345, 50001) is None
        assert original_data == {"step": "ready", "nested": {"items": ["kept"]}}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_failed_continuation_cleanup_does_not_delete_replacement_generation(
        self,
        test_dispatcher,
    ):
        """A replacement committed after failure wins over conditional cleanup."""

        dispatcher, session_manager = test_dispatcher
        original = await session_manager.create(12345, 50001, "stateful", {"step": "old"})
        cleanup_waiting = asyncio.Event()
        allow_cleanup = asyncio.Event()
        real_update = session_manager.update
        update_calls = 0

        async def delay_cleanup_update(user_id, group_id, callback):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 2:
                cleanup_waiting.set()
                await allow_cleanup.wait()
            return await real_update(user_id, group_id, callback)

        session_manager.update = delay_cleanup_update

        async def handle_session(_text, _event, _context, session):
            session.set("step", "partial")
            raise RuntimeError("continuation failed")

        close_session = MagicMock()
        loaded = SimpleNamespace(
            module=SimpleNamespace(
                handle_session=handle_session,
                close_session=close_session,
            ),
            execution_gate=None,
        )
        dispatcher.plugin_registry.get.side_effect = lambda name: (
            loaded if name == "stateful" else None
        )
        context = MessageContext(
            request_id="replacement-after-failure",
            text="continue",
            clean_text="continue",
            user_id=12345,
            group_id=50001,
            is_private=False,
            has_bot_name=False,
            has_prefix=False,
            has_command_prefix=False,
            is_only_bot_name=False,
            is_at_me=False,
            is_url_only=False,
            event={"user_id": 12345, "group_id": 50001},
        )

        dispatch_task = asyncio.create_task(dispatcher._try_handle_session(context))
        await cleanup_waiting.wait()
        replacement = await session_manager.create(
            12345,
            50001,
            "stateful",
            {"step": "replacement"},
        )
        allow_cleanup.set()
        response = await dispatch_task

        current = await session_manager.peek(12345, 50001)
        assert response
        assert current is not None
        assert current.session_id == replacement.session_id
        assert current.session_id != original.session_id
        assert current.get("step") == "replacement"
        close_session.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_generation_revalidation_retries_same_plugin_replacement(
        self,
        test_dispatcher,
    ):
        dispatcher, session_manager = test_dispatcher
        original = await session_manager.create(12345, 50001, "stateful", {"step": "old"})
        first_snapshot_taken = asyncio.Event()
        allow_first_snapshot = asyncio.Event()
        real_peek = session_manager.peek
        peek_calls = 0

        async def pause_first_peek(user_id, group_id):
            nonlocal peek_calls
            snapshot = await real_peek(user_id, group_id)
            peek_calls += 1
            if peek_calls == 1:
                first_snapshot_taken.set()
                await allow_first_snapshot.wait()
            return snapshot

        session_manager.peek = pause_first_peek
        handled_session_ids: list[str] = []

        async def handle_session(_text, _event, _context, session):
            handled_session_ids.append(session.session_id)
            return []

        loaded = SimpleNamespace(
            module=SimpleNamespace(handle_session=handle_session),
            execution_gate=None,
        )
        dispatcher.plugin_registry.get.side_effect = lambda name: (
            loaded if name == "stateful" else None
        )
        context = MessageContext(
            request_id="same-plugin-replacement",
            text="continue",
            clean_text="continue",
            user_id=12345,
            group_id=50001,
            is_private=False,
            has_bot_name=False,
            has_prefix=False,
            has_command_prefix=False,
            is_only_bot_name=False,
            is_at_me=False,
            is_url_only=False,
            event={"user_id": 12345, "group_id": 50001},
        )

        dispatch_task = asyncio.create_task(dispatcher._try_handle_session(context))
        await first_snapshot_taken.wait()
        replacement = await session_manager.create(
            12345,
            50001,
            "stateful",
            {"step": "new"},
        )
        allow_first_snapshot.set()

        assert await dispatch_task == []
        assert replacement.session_id != original.session_id
        assert handled_session_ids == [replacement.session_id]
        current = await real_peek(12345, 50001)
        assert current is not None
        assert current.version == replacement.version + 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_plugin_gate_precedes_session_lock_to_avoid_abba_deadlock(
        self,
        test_dispatcher,
    ):
        dispatcher, session_manager = test_dispatcher
        await session_manager.create(12345, 50001, "stateful")
        gate = PluginExecutionGate("sequential", plugin_name="stateful")
        loaded = SimpleNamespace(
            module=SimpleNamespace(handle_session=MagicMock()),
            execution_gate=gate,
        )
        dispatcher.plugin_registry.get.side_effect = lambda name: (
            loaded if name == "stateful" else None
        )
        command_holds_gate = asyncio.Event()
        allow_command_delete = asyncio.Event()

        async def command_operation() -> bool:
            command_holds_gate.set()
            await allow_command_delete.wait()
            return await session_manager.delete(12345, 50001)

        command_task = asyncio.create_task(gate.run(command_operation))
        await command_holds_gate.wait()
        context = MessageContext(
            request_id="abba-regression",
            text="continue",
            clean_text="continue",
            user_id=12345,
            group_id=50001,
            is_private=False,
            has_bot_name=False,
            has_prefix=False,
            has_command_prefix=False,
            is_only_bot_name=False,
            is_at_me=False,
            is_url_only=False,
            event={"user_id": 12345, "group_id": 50001},
        )
        session_task = asyncio.create_task(dispatcher._try_handle_session(context))
        await asyncio.sleep(0)

        allow_command_delete.set()
        command_result, session_result = await asyncio.wait_for(
            asyncio.gather(command_task, session_task),
            timeout=1,
        )

        assert command_result is True
        assert session_result is None
        assert session_manager._key_lock_pool.active_key_count == 0


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
