"""
SessionManager 单元测试
"""

import asyncio

import pytest

from core.session import Session, SessionManager

# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def session_manager() -> SessionManager:
    """创建会话管理器"""
    return SessionManager(default_timeout=300.0)

# ============================================================
# Session 类测试
# ============================================================

class TestSession:
    """Session 类测试"""

    def test_session_creation(self):
        """测试会话创建"""
        session = Session(
            user_id=12345,
            group_id=67890,
            plugin_name="guess_number",
        )
        assert session.user_id == 12345
        assert session.group_id == 67890
        assert session.plugin_name == "guess_number"
        assert session.state == "active"
        assert session.data == {}

    def test_session_get_set(self):
        """测试会话数据读写"""
        session = Session(user_id=1, group_id=None, plugin_name="test")
        
        # 设置数据
        session.set("target", 42)
        session.set("attempts", 0)
        
        # 读取数据
        assert session.get("target") == 42
        assert session.get("attempts") == 0
        assert session.get("nonexistent") is None
        assert session.get("nonexistent", "default") == "default"

    def test_session_clear(self):
        """测试清空会话数据"""
        session = Session(user_id=1, group_id=None, plugin_name="test")
        session.set("key1", "value1")
        session.set("key2", "value2")
        
        session.clear()
        
        assert session.get("key1") is None
        assert session.get("key2") is None
        assert session.data == {}

    def test_session_expiry(self):
        """测试会话过期检测"""
        import time
        
        # 创建一个超短超时的会话
        session = Session(
            user_id=1,
            group_id=None,
            plugin_name="test",
            timeout=0.05,  # 50ms
        )
        
        assert not session.is_expired()
        
        # 等待超时
        time.sleep(0.1)
        
        assert session.is_expired()

    def test_session_update_resets_timeout(self):
        """测试更新会话会重置超时"""
        import time
        
        session = Session(
            user_id=1,
            group_id=None,
            plugin_name="test",
            timeout=0.2,  # 200ms
        )
        
        # 等待一段时间（但不超时）
        time.sleep(0.1)
        
        # 更新会话
        session.update()
        
        # 再等待一段时间
        time.sleep(0.1)
        
        # 由于刚更新过，不应该过期
        assert not session.is_expired()

# ============================================================
# SessionManager 类测试
# ============================================================

class TestSessionManager:
    """SessionManager 类测试"""

    @pytest.mark.asyncio
    async def test_create_session(self, session_manager: SessionManager):
        """测试创建会话"""
        session = await session_manager.create(
            user_id=12345,
            group_id=67890,
            plugin_name="guess_number",
            initial_data={"target": 50},
        )
        
        assert session.user_id == 12345
        assert session.group_id == 67890
        assert session.plugin_name == "guess_number"
        assert session.get("target") == 50

    @pytest.mark.asyncio
    async def test_get_session(self, session_manager: SessionManager):
        """测试获取会话"""
        await session_manager.create(
            user_id=12345,
            group_id=67890,
            plugin_name="test",
        )
        
        session = await session_manager.get(12345, 67890)
        assert session is not None
        assert session.user_id == 12345

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self, session_manager: SessionManager):
        """测试获取不存在的会话"""
        session = await session_manager.get(99999, None)
        assert session is None

    @pytest.mark.asyncio
    async def test_delete_session(self, session_manager: SessionManager):
        """测试删除会话"""
        await session_manager.create(
            user_id=12345,
            group_id=None,
            plugin_name="test",
        )
        
        # 确认存在
        assert await session_manager.exists(12345, None)
        
        # 删除
        result = await session_manager.delete(12345, None)
        assert result is True
        
        # 确认不存在
        assert not await session_manager.exists(12345, None)

    @pytest.mark.asyncio
    async def test_exists_does_not_refresh_session_timeout(self, session_manager: SessionManager):
        """测试 exists 只检查存在性，不刷新会话时间戳"""
        session = await session_manager.create(
            user_id=12345,
            group_id=None,
            plugin_name="test",
            timeout=10.0,
        )
        await asyncio.sleep(0.02)
        before = session.updated_at

        assert await session_manager.exists(12345, None) is True
        assert session.updated_at == before

    @pytest.mark.asyncio
    async def test_peek_does_not_refresh_session_timeout(self, session_manager: SessionManager):
        """测试 peek 返回会话但不刷新会话时间戳"""
        session = await session_manager.create(
            user_id=12345,
            group_id=None,
            plugin_name="test",
            timeout=10.0,
        )
        await asyncio.sleep(0.02)
        before = session.updated_at

        peeked = await session_manager.peek(12345, None)

        assert peeked is session
        assert session.updated_at == before

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self, session_manager: SessionManager):
        """测试删除不存在的会话"""
        result = await session_manager.delete(99999, None)
        assert result is False

    @pytest.mark.asyncio
    async def test_session_isolation_by_group(self, session_manager: SessionManager):
        """测试同一用户在不同群的会话隔离"""
        # 用户在群 A 的会话
        await session_manager.create(
            user_id=12345,
            group_id=100,
            plugin_name="game_a",
            initial_data={"score": 10},
        )
        
        # 同一用户在群 B 的会话
        await session_manager.create(
            user_id=12345,
            group_id=200,
            plugin_name="game_b",
            initial_data={"score": 20},
        )
        
        # 验证隔离
        retrieved_a = await session_manager.get(12345, 100)
        retrieved_b = await session_manager.get(12345, 200)
        
        assert retrieved_a.get("score") == 10
        assert retrieved_b.get("score") == 20
        assert retrieved_a.plugin_name == "game_a"
        assert retrieved_b.plugin_name == "game_b"

    @pytest.mark.asyncio
    async def test_session_private_vs_group(self, session_manager: SessionManager):
        """测试私聊会话和群聊会话隔离"""
        # 私聊会话
        await session_manager.create(
            user_id=12345,
            group_id=None,
            plugin_name="private_game",
        )
        
        # 群聊会话
        await session_manager.create(
            user_id=12345,
            group_id=100,
            plugin_name="group_game",
        )
        
        private = await session_manager.get(12345, None)
        group = await session_manager.get(12345, 100)
        
        assert private.plugin_name == "private_game"
        assert group.plugin_name == "group_game"

    @pytest.mark.asyncio
    async def test_create_overwrites_existing(self, session_manager: SessionManager):
        """测试创建会话会覆盖已存在的会话"""
        await session_manager.create(
            user_id=12345,
            group_id=None,
            plugin_name="old_plugin",
            initial_data={"old_key": "old_value"},
        )
        
        await session_manager.create(
            user_id=12345,
            group_id=None,
            plugin_name="new_plugin",
            initial_data={"new_key": "new_value"},
        )
        
        session = await session_manager.get(12345, None)
        assert session.plugin_name == "new_plugin"
        assert session.get("new_key") == "new_value"
        assert session.get("old_key") is None

    @pytest.mark.asyncio
    async def test_count(self, session_manager: SessionManager):
        """测试会话计数"""
        assert await session_manager.count() == 0
        
        await session_manager.create(1, None, "test")
        assert await session_manager.count() == 1
        
        await session_manager.create(2, None, "test")
        assert await session_manager.count() == 2
        
        await session_manager.delete(1, None)
        assert await session_manager.count() == 1

    @pytest.mark.asyncio
    async def test_list_user_sessions(self, session_manager: SessionManager):
        """测试列出用户所有会话"""
        await session_manager.create(12345, None, "private")
        await session_manager.create(12345, 100, "group1")
        await session_manager.create(12345, 200, "group2")
        await session_manager.create(99999, None, "other_user")
        
        sessions = await session_manager.list_user_sessions(12345)
        assert len(sessions) == 3

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, session_manager: SessionManager):
        """测试清理过期会话"""
        import time
        
        # 创建一个即将过期的会话
        await session_manager.create(
            user_id=1,
            group_id=None,
            plugin_name="test",
            timeout=0.01,
        )
        
        # 创建一个不会过期的会话
        await session_manager.create(
            user_id=2,
            group_id=None,
            plugin_name="test",
            timeout=300.0,
        )
        
        # 等待第一个过期
        time.sleep(0.02)
        
        # 清理
        cleaned = await session_manager.cleanup_expired()
        assert cleaned == 1
        
        # 验证
        assert await session_manager.get(1, None) is None
        assert await session_manager.get(2, None) is not None

# ============================================================
# Session 超时和并发测试
# ============================================================

class TestSessionTimeout:
    """测试会话超时机制"""

    @pytest.mark.asyncio
    async def test_session_expiry(self):
        """测试会话过期"""
        manager = SessionManager(default_timeout=0.1)  # 100ms超时

        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={"state": "active"}
        )
        session = await manager.get(10001, 50001)
        assert session is not None

        # 等待超时
        await asyncio.sleep(0.15)

        # 清理过期会话
        cleaned = await manager.cleanup_expired()
        assert cleaned == 1

        # 验证会话已不存在
        session = await manager.get(10001, 50001)
        assert session is None

    @pytest.mark.asyncio
    async def test_session_refresh(self):
        """测试会话刷新"""
        manager = SessionManager(default_timeout=0.2)

        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test"
        )

        # 在超时前刷新
        await asyncio.sleep(0.15)
        session = await manager.get(10001, 50001)
        if session:
            session.update()  # 刷新会话

        # 再次等待后应该仍然存在
        await asyncio.sleep(0.1)
        session = await manager.get(10001, 50001)
        assert session is not None

    @pytest.mark.asyncio
    async def test_concurrent_session_access(self):
        """同键 update 事务不会丢失跨 await 的读改写。"""
        manager = SessionManager()

        await manager.create(
            user_id=10001,
            group_id=50001,
            plugin_name="test",
            initial_data={"counter": 0}
        )

        async def increment():
            for _ in range(100):
                async def increment_once(session):
                    value = session.get("counter", 0)
                    await asyncio.sleep(0)
                    session.set("counter", value + 1)

                await manager.update(10001, 50001, increment_once)

        # 并发执行
        await asyncio.gather(increment(), increment())

        session = await manager.get(10001, 50001)
        assert session is not None
        assert session.data["counter"] == 200

    @pytest.mark.asyncio
    async def test_update_keeps_different_session_keys_parallel(self):
        manager = SessionManager()
        await manager.create(1, 1, "test")
        await manager.create(2, 2, "test")
        both_entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def slow_update(_session):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_entered.set()
            await release.wait()
            active -= 1

        first = asyncio.create_task(manager.update(1, 1, slow_update))
        second = asyncio.create_task(manager.update(2, 2, slow_update))
        await both_entered.wait()
        assert max_active == 2
        release.set()
        await asyncio.gather(first, second)

    @pytest.mark.asyncio
    async def test_cleanup_waits_for_active_transaction_then_keeps_refreshed_session(self):
        manager = SessionManager(default_timeout=0.01)
        await manager.create(1, 1, "test")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_update(_session):
            entered.set()
            await release.wait()

        update_task = asyncio.create_task(manager.update(1, 1, slow_update))
        await entered.wait()
        await asyncio.sleep(0.02)
        cleanup_task = asyncio.create_task(manager.cleanup_expired())
        await asyncio.sleep(0)
        assert not cleanup_task.done()

        release.set()
        await update_task
        assert await cleanup_task == 0
        assert await manager.exists(1, 1) is True

    @pytest.mark.asyncio
    async def test_update_allows_handler_to_end_its_own_session(self):
        manager = SessionManager()
        await manager.create(1, 1, "test")

        async def end_session(_session):
            return await manager.delete(1, 1)

        assert await manager.update(1, 1, end_session) is True
        assert await manager.exists(1, 1) is False

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
