"""Test complete session lifecycle"""

import asyncio

import pytest

from core.session import SessionManager


class TestSessionLifecycle:
    """Test session lifecycle"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_session_lifecycle(self):
        """Test complete session cycle: create -> use -> timeout -> cleanup"""
        manager = SessionManager(default_timeout=0.5)

        # Create session
        session = await manager.create(
            user_id      = 10001,
            group_id     = 50001,
            plugin_name  = "test_plugin",
            initial_data = {"step": 1, "value": None},
        )
        assert session is not None

        # Verify session exists
        retrieved = await manager.get(10001, 50001)
        assert retrieved is not None
        assert retrieved.data["step"] == 1

        # Update through the transaction boundary. Retrieved is a snapshot.
        def update_values(working):
            working.data["step"]  = 2
            working.data["value"] = "test"

        await manager.update(10001, 50001, update_values)

        # Verify updates persisted (session still valid)
        updated = await manager.get(10001, 50001)
        assert updated is not None
        assert updated.data["step"] == 2
        assert updated.data["value"] == "test"

        # Wait for timeout
        await asyncio.sleep(0.6)

        # Cleanup expired sessions
        cleaned = await manager.cleanup_expired()
        assert cleaned == 1

        # Verify session is gone
        expired = await manager.get(10001, 50001)
        assert expired is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_multi_user_sessions(self):
        """Test multi-user session isolation"""
        manager = SessionManager()

        # Create sessions for different users
        session1 = await manager.create(10001, 50001, "plugin_a", {"user": "A"})
        session2 = await manager.create(10002, 50001, "plugin_b", {"user": "B"})
        session3 = await manager.create(10001, 50002, "plugin_c", {"user": "C"})

        assert session1.user_id == 10001
        assert session2.user_id == 10002
        assert session3.user_id == 10001

        # Verify isolation by key
        s1 = await manager.get(10001, 50001)
        s2 = await manager.get(10002, 50001)
        s3 = await manager.get(10001, 50002)

        assert s1.data.get("user") == "A"
        assert s2.data.get("user") == "B"
        assert s3.data.get("user") == "C"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_state_management(self):
        """Test session state field and lifecycle"""
        manager = SessionManager()

        session = await manager.create(user_id=10001, group_id=50001, plugin_name="test")

        # Default state
        assert session.state == "active"

        # Change state through the transaction boundary.
        def pause(working):
            working.state = "paused"

        await manager.update(10001, 50001, pause)

        # Verify state persists
        retrieved = await manager.get(10001, 50001)
        assert retrieved.state == "paused"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_all_sessions_by_plugin(self):
        """Test filtering sessions by plugin name"""
        manager = SessionManager()

        await manager.create(1, None, "plugin_a", initial_data={"x": 1})
        await manager.create(2, None, "plugin_a", initial_data={"x": 2})
        await manager.create(3, None, "plugin_b", initial_data={"x": 3})
        await manager.create(4, None, "plugin_a", initial_data={"x": 4})
        await manager.create(5, None, "plugin_b", initial_data={"x": 5})

        # Get all sessions for plugin_a
        plugin_a_sessions = await manager.get_all_sessions("plugin_a")
        assert len(plugin_a_sessions) == 3

        # Get all sessions for plugin_b
        plugin_b_sessions = await manager.get_all_sessions("plugin_b")
        assert len(plugin_b_sessions) == 2

        # Get all sessions
        all_sessions = await manager.get_all_sessions()
        assert len(all_sessions) == 5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_custom_timeout(self):
        """Test sessions with custom timeout values"""
        manager = SessionManager(default_timeout=1.0)  # Default 1 second

        # Create session with custom short timeout
        await manager.create(user_id=1, group_id=None, plugin_name="test", timeout=0.1)

        # Create session with default timeout
        await manager.create(user_id=2, group_id=None, plugin_name="test")

        # Wait for short timeout session to expire
        await asyncio.sleep(0.15)

        # First should be expired
        s1 = await manager.get(1, None)
        assert s1 is None

        # Second should still be valid
        s2 = await manager.get(2, None)
        assert s2 is not None


class TestSessionDataOperations:
    """Test session data operations"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_get_set_operations(self):
        """Test get/set/clear operations on session data"""
        manager = SessionManager()

        session = await manager.create(
            user_id      = 10001,
            group_id     = 50001,
            plugin_name  = "test",
            initial_data = {"key1": "value1", "key2": 42},
        )

        # Test get
        assert session.get("key1") == "value1"
        assert session.get("key2") == 42
        assert session.get("nonexistent") is None
        assert session.get("nonexistent", "default") == "default"

        # Test set
        session.set("key3", "new_value")
        assert session.get("key3") == "new_value"

        # Windows 时钟允许同一 tick 内连续读数相等；用版本号验证 touch 确实发生。
        previous_updated_at = session.updated_at
        previous_version    = session.version
        session.update()
        assert session.updated_at >= previous_updated_at >= session.created_at
        assert session.version == previous_version + 1

        # Test clear
        session.clear()
        assert session.get("key1") is None
        assert session.get("key2") is None
        assert session.get("key3") is None
