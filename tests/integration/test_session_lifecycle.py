"""Test complete session lifecycle"""

import asyncio
import pytest
from typing import Any

from core.session import Session, SessionManager


class TestSessionLifecycle:
    """Test session lifecycle"""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_full_session_lifecycle(self):
        """Test complete session cycle: create -> use -> timeout -> cleanup"""
        manager = SessionManager(default_timeout=0.5)

        # Create session
        session = await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test_plugin",
            initial_data={"step": 1, "value": None}
        )
        assert session is not None

        # Verify session exists
        retrieved = await manager.get(10001, 50001)
        assert retrieved is not None
        assert retrieved.data["step"] == 1

        # Update session data
        retrieved.data["step"] = 2
        retrieved.data["value"] = "test"
        retrieved.update()  # Refresh timestamp

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
    async def test_private_vs_group_sessions(self):
        """Test private chat and group chat sessions are isolated"""
        manager = SessionManager()

        # Create private session
        private_session = await manager.create(
            user_id=10001,
            group_id=None,
            plugin_name="private_game",
            initial_data={"mode": "private"}
        )

        # Create group session for same user
        group_session = await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="group_game",
            initial_data={"mode": "group"}
        )

        # Verify both exist independently
        private_retrieved = await manager.get(10001, None)
        group_retrieved = await manager.get(10001, 50001)

        assert private_retrieved is not None
        assert group_retrieved is not None
        assert private_retrieved.plugin_name == "private_game"
        assert group_retrieved.plugin_name == "group_game"
        assert private_retrieved.data["mode"] == "private"
        assert group_retrieved.data["mode"] == "group"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_data_persistence(self):
        """Test that session data persists across accesses"""
        manager = SessionManager(default_timeout=5.0)

        # Create session with initial data
        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={"counter": 0, "history": []}
        )

        # First access - modify data
        session1 = await manager.get(10001, 50001)
        session1.data["counter"] = 1
        session1.data["history"].append("action1")

        # Second access - verify data persists
        session2 = await manager.get(10001, 50001)
        assert session2.data["counter"] == 1
        assert session2.data["history"] == ["action1"]

        # Third access - modify again
        session2.data["counter"] = 2
        session2.data["history"].append("action2")

        # Fourth access - verify all changes
        session3 = await manager.get(10001, 50001)
        assert session3.data["counter"] == 2
        assert session3.data["history"] == ["action1", "action2"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_deletion(self):
        """Test session deletion and lifecycle"""
        manager = SessionManager()

        # Create session
        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={"active": True}
        )

        # Verify exists
        assert await manager.exists(10001, 50001)
        assert await manager.count() == 1

        # Delete session
        deleted = await manager.delete(10001, 50001)
        assert deleted is True

        # Verify deleted
        assert not await manager.exists(10001, 50001)
        assert await manager.count() == 0

        # Delete non-existent session
        deleted_again = await manager.delete(10001, 50001)
        assert deleted_again is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_timeout_refresh(self):
        """Test that accessing a session refreshes its timeout"""
        manager = SessionManager(default_timeout=0.3)

        # Create session
        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test"
        )

        # Wait half the timeout
        await asyncio.sleep(0.15)

        # Access and update the session (refreshes timeout)
        session = await manager.get(10001, 50001)
        if session:
            session.update()

        # Wait for original timeout to pass
        await asyncio.sleep(0.2)

        # Session should still exist because we refreshed it
        still_valid = await manager.get(10001, 50001)
        assert still_valid is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_concurrent_session_operations(self):
        """Test concurrent session access and modifications"""
        manager = SessionManager()

        # Create session
        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={"counter": 0}
        )

        async def increment_session():
            """Increment session counter multiple times"""
            for _ in range(10):
                session = await manager.get(10001, 50001)
                if session:
                    session.data["counter"] += 1
                await asyncio.sleep(0.001)

        # Run concurrent operations
        await asyncio.gather(
            increment_session(),
            increment_session(),
            increment_session(),
        )

        # Verify final count
        final_session = await manager.get(10001, 50001)
        assert final_session is not None
        assert final_session.data["counter"] == 30

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_user_sessions(self):
        """Test listing all sessions for a user"""
        manager = SessionManager()

        # Create multiple sessions for same user
        await manager.create(10001, None, "private")
        await manager.create(10001, 100, "group1")
        await manager.create(10001, 200, "group2")
        await manager.create(10001, 300, "group3")
        # Create sessions for other users
        await manager.create(10002, None, "other_private")
        await manager.create(10002, 100, "other_group")

        # List user's sessions
        user_sessions = await manager.list_user_sessions(10001)
        assert len(user_sessions) == 4

        # Verify each session
        plugin_names = {s.plugin_name for s in user_sessions}
        assert plugin_names == {"private", "group1", "group2", "group3"}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_cleanup_expired_sessions(self):
        """Test cleaning up multiple expired sessions"""
        manager = SessionManager(default_timeout=0.2)

        # Create sessions with different timeouts
        await manager.create(1, None, "test", timeout=0.1)  # Expires first
        await manager.create(2, None, "test", timeout=0.1)  # Expires first
        await manager.create(3, None, "test", timeout=0.3)  # Still valid
        await manager.create(4, None, "test", timeout=0.1)  # Expires first
        await manager.create(5, None, "test", timeout=0.3)  # Still valid

        # Wait for first batch to expire
        await asyncio.sleep(0.15)

        # Cleanup
        cleaned = await manager.cleanup_expired()
        assert cleaned == 3

        # Verify correct sessions remain
        assert await manager.get(1, None) is None
        assert await manager.get(2, None) is None
        assert await manager.get(3, None) is not None
        assert await manager.get(4, None) is None
        assert await manager.get(5, None) is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_state_management(self):
        """Test session state field and lifecycle"""
        manager = SessionManager()

        session = await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test"
        )

        # Default state
        assert session.state == "active"

        # Change state
        session.state = "paused"
        session.update()

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
        await manager.create(
            user_id=1,
            group_id=None,
            plugin_name="test",
            timeout=0.1
        )

        # Create session with default timeout
        await manager.create(
            user_id=2,
            group_id=None,
            plugin_name="test"
        )

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
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={"key1": "value1", "key2": 42}
        )

        # Test get
        assert session.get("key1") == "value1"
        assert session.get("key2") == 42
        assert session.get("nonexistent") is None
        assert session.get("nonexistent", "default") == "default"

        # Test set
        session.set("key3", "new_value")
        assert session.get("key3") == "new_value"

        # Test update
        session.update()
        assert session.updated_at > session.created_at

        # Test clear
        session.clear()
        assert session.get("key1") is None
        assert session.get("key2") is None
        assert session.get("key3") is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_data_with_complex_types(self):
        """Test session data with lists, dicts, and nested structures"""
        manager = SessionManager()

        session = await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={
                "list": [1, 2, 3],
                "dict": {"nested": "value"},
                "empty_list": [],
            }
        )

        # Modify list
        session.data["list"].append(4)
        assert session.data["list"] == [1, 2, 3, 4]

        # Modify nested dict
        session.data["dict"]["new_key"] = "new_value"
        assert session.data["dict"]["new_key"] == "new_value"

        # Verify persistence
        retrieved = await manager.get(10001, 50001)
        assert retrieved.data["list"] == [1, 2, 3, 4]
        assert retrieved.data["dict"]["new_key"] == "new_value"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration"])
