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

        assert peeked is not session
        assert peeked == session
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
            user_id=10001, group_id=50001, plugin_name="test", initial_data={"state": "active"}
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

        await manager.create(user_id=10001, group_id=50001, plugin_name="test")

        # 在超时前读取；get() 会刷新正式值的空闲租约。
        await asyncio.sleep(0.15)
        session = await manager.get(10001, 50001)
        assert session is not None

        # 再次等待后应该仍然存在
        await asyncio.sleep(0.1)
        session = await manager.get(10001, 50001)
        assert session is not None

    @pytest.mark.asyncio
    async def test_concurrent_session_access(self):
        """同键 update 事务不会丢失跨 await 的读改写。"""
        manager = SessionManager()

        await manager.create(
            user_id=10001, group_id=50001, plugin_name="test", initial_data={"counter": 0}
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

    @pytest.mark.asyncio
    async def test_high_cardinality_missing_sessions_leave_no_key_locks(self):
        manager = SessionManager()

        await asyncio.gather(*(manager.exists(user_id, None) for user_id in range(1000, 2000)))
        await asyncio.gather(*(manager.peek(user_id, user_id) for user_id in range(2000, 3000)))

        assert manager.active_count == 0
        assert manager.active_key_lock_count == 0


class TestSessionTransactions:
    """Regression coverage for isolated snapshots and true update transactions."""

    @pytest.mark.asyncio
    async def test_create_and_get_return_detached_snapshots(self):
        manager = SessionManager()
        initial = {"nested": {"values": [1]}}

        created = await manager.create(1, None, "test", initial)
        initial["nested"]["values"].append(2)
        created.data["nested"]["values"].append(3)

        first = await manager.get(1, None)
        assert first is not None
        assert first.data == {"nested": {"values": [1]}}
        first.data["nested"]["values"].append(4)

        second = await manager.peek(1, None)
        assert second is not None
        assert second.data == {"nested": {"values": [1]}}
        assert second.session_id == created.session_id

        listed = await manager.list_user_sessions(1)
        all_sessions = await manager.get_all_sessions("test")
        listed[0].data["nested"]["values"].append(5)
        all_sessions[0].data["nested"]["values"].append(6)
        assert (await manager.peek(1, None)).data == {"nested": {"values": [1]}}

    @pytest.mark.asyncio
    async def test_get_refreshes_only_stored_idle_timestamp(self):
        manager = SessionManager()
        await manager.create(1, None, "test", {"value": 1})
        before = await manager.peek(1, None)
        assert before is not None
        await asyncio.sleep(0.001)

        returned = await manager.get(1, None)
        stored = await manager.peek(1, None)
        assert returned is not None and stored is not None
        assert stored.updated_at > before.updated_at
        assert stored.version == before.version

        returned.updated_at = 0
        returned.version = 999
        unchanged = await manager.peek(1, None)
        assert unchanged is not None
        assert unchanged.updated_at == stored.updated_at
        assert unchanged.version == stored.version

    @pytest.mark.asyncio
    async def test_create_copy_failure_never_publishes_a_session(self):
        class CannotCopy:
            def __deepcopy__(self, memo):
                raise TypeError("initial copy failed")

        manager = SessionManager()
        with pytest.raises(TypeError, match="session data values"):
            await manager.create(1, None, "test", {"bad": CannotCopy()})
        assert await manager.exists(1, None) is False
        assert manager.active_key_lock_count == 0

    @pytest.mark.asyncio
    async def test_exception_and_base_exception_roll_back_value_and_metadata(self):
        class StopTransaction(BaseException):
            pass

        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)
        assert original is not None

        async def fail_with_exception(session):
            session.set("value", 2)
            session.state = "changed"
            raise ValueError("rollback")

        with pytest.raises(ValueError, match="rollback"):
            await manager.update(1, 2, fail_with_exception)
        assert await manager.peek(1, 2) == original

        async def fail_with_base_exception(session):
            session.clear()
            raise StopTransaction

        with pytest.raises(StopTransaction):
            await manager.update(1, 2, fail_with_base_exception)
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_swallowed_callback_cancellation_still_rolls_back_and_propagates(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)
        entered = asyncio.Event()
        blocker = asyncio.Event()

        async def swallow_cancel(session):
            session.set("value", 2)
            entered.set()
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                session.set("value", 3)
                return "must-not-commit"

        task = asyncio.create_task(manager.update(1, 2, swallow_cancel))
        await entered.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_success_commits_once_and_detaches_retained_callback_reference(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"nested": {"values": []}})
        original = await manager.peek(1, 2)
        assert original is not None
        retained = []

        async def mutate(session):
            retained.append(session)
            session.set("first", True)
            session.set("second", True)
            session.data["nested"]["values"].append("committed")
            session.version = 9999
            session.user_id = 999
            session.group_id = 999
            session.plugin_name = "forged-plugin"
            session.session_id = "forged"
            return "ok"

        assert await manager.update(1, 2, mutate) == "ok"
        committed = await manager.peek(1, 2)
        assert committed is not None
        assert committed.version == original.version + 1
        # Windows 时钟可能在一次事务内落在同一刻度；版本递增才是提交顺序。
        assert committed.updated_at >= original.updated_at
        assert committed.created_at == original.created_at
        assert committed.user_id == 1
        assert committed.group_id == 2
        assert committed.plugin_name == "test"
        assert committed.session_id == original.session_id

        retained[0].data["nested"]["values"].append("late mutation")
        assert (await manager.peek(1, 2)).data["nested"]["values"] == ["committed"]

    @pytest.mark.asyncio
    async def test_returned_working_value_and_nested_references_are_detached(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"nested": {"values": []}})

        def expose_working(session):
            session.data["nested"]["values"].append("committed")
            return session, session.data["nested"]

        returned, nested = await manager.update(1, 2, expose_working)
        returned.data["nested"]["values"].append("late-session")
        nested["values"].append("late-nested")

        committed = await manager.peek(1, 2)
        assert committed is not None
        assert committed.data["nested"]["values"] == ["committed"]

    @pytest.mark.asyncio
    async def test_commit_deepcopy_failure_rolls_back(self):
        class CannotCopy:
            def __deepcopy__(self, memo):
                raise TypeError("cannot copy")

        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)

        def add_uncopyable(session):
            session.data["bad"] = CannotCopy()

        with pytest.raises(TypeError, match="session data values"):
            await manager.update(1, 2, add_uncopyable)
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_custom_copy_hooks_cannot_bypass_snapshot_or_rollback_isolation(self):
        class Sticky:
            def __init__(self):
                self.value = 1

            def __deepcopy__(self, _memo):
                return self

        manager = SessionManager()
        with pytest.raises(TypeError, match="session data values"):
            await manager.create(1, 2, "test", {"sticky": Sticky()})
        assert await manager.peek(1, 2) is None

        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)

        def inject_custom_value(session):
            session.data["sticky"] = Sticky()

        with pytest.raises(TypeError, match="session data values"):
            await manager.update(1, 2, inject_custom_value)
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_same_task_reentrant_operations_use_staged_view(self):
        manager = SessionManager()
        original_created = await manager.create(1, 2, "old", {"value": 1})
        staged_ids = []

        async def replace(session):
            session.data["value"] = 2
            visible = await manager.get(1, 2)
            assert visible is not None and visible.data["value"] == 2
            visible.data["value"] = 999
            assert (await manager.peek(1, 2)).data["value"] == 2
            assert await manager.exists(1, 2) is True

            assert await manager.delete(1, 2) is True
            assert await manager.delete(1, 2) is False
            assert await manager.get(1, 2) is None
            assert await manager.peek(1, 2) is None
            assert await manager.exists(1, 2) is False

            replacement = await manager.create("1", "2", " new ", {"value": 3})
            staged_ids.append(replacement.session_id)
            replacement.data["value"] = 999
            staged = await manager.get(1, 2)
            assert staged is not None
            assert staged.plugin_name == "new"
            assert staged.data["value"] == 3

        await manager.update(1, 2, replace)
        committed = await manager.peek(1, 2)
        assert committed is not None
        assert committed.data["value"] == 3
        assert committed.session_id == staged_ids[0]
        assert committed.session_id != original_created.session_id

    @pytest.mark.asyncio
    async def test_staged_delete_and_replace_roll_back_on_failure(self):
        manager = SessionManager()
        await manager.create(1, 2, "old", {"value": 1})
        original = await manager.peek(1, 2)

        async def delete_then_fail(_session):
            assert await manager.delete(1, 2) is True
            assert await manager.exists(1, 2) is False
            raise RuntimeError("after delete")

        with pytest.raises(RuntimeError, match="after delete"):
            await manager.update(1, 2, delete_then_fail)
        assert await manager.peek(1, 2) == original

        async def replace_then_fail(_session):
            assert await manager.delete(1, 2) is True
            replacement = await manager.create(1, 2, "new", {"value": 2})
            assert replacement.session_id != original.session_id
            raise RuntimeError("after replace")

        with pytest.raises(RuntimeError, match="after replace"):
            await manager.update(1, 2, replace_then_fail)
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_staged_delete_rolls_back_on_swallowed_cancellation(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)
        entered = asyncio.Event()

        async def delete_then_swallow(_session):
            await manager.delete(1, 2)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "swallowed"

        task = asyncio.create_task(manager.update(1, 2, delete_then_swallow))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_nested_same_key_update_is_rejected(self):
        manager = SessionManager()
        await manager.create(1, 2, "test")

        async def outer(_session):
            with pytest.raises(RuntimeError, match="nested session update"):
                await manager.update(1, 2, lambda session: session.set("bad", True))

        await manager.update(1, 2, outer)
        committed = await manager.peek(1, 2)
        assert committed is not None
        assert "bad" not in committed.data

    @pytest.mark.asyncio
    async def test_cross_key_callbacks_fail_fast_instead_of_deadlocking(self):
        manager = SessionManager()
        await manager.create(1, 10, "test", {"value": "first"})
        await manager.create(2, 20, "test", {"value": "second"})
        originals = (
            await manager.peek(1, 10),
            await manager.peek(2, 20),
        )
        both_entered = asyncio.Event()
        entered_count = 0

        async def cross_update(_session, target_user, target_group):
            nonlocal entered_count
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
            await both_entered.wait()
            await manager.update(
                target_user,
                target_group,
                lambda target: target.set("cross", True),
            )

        tasks = (
            asyncio.create_task(
                manager.update(1, 10, lambda session: cross_update(session, 2, 20))
            ),
            asyncio.create_task(
                manager.update(2, 20, lambda session: cross_update(session, 1, 10))
            ),
        )
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=1,
        )

        assert all(isinstance(result, RuntimeError) for result in results)
        assert all("different session key" in str(result) for result in results)
        assert await manager.peek(1, 10) == originals[0]
        assert await manager.peek(2, 20) == originals[1]
        assert manager._transactions == {}
        assert manager.active_key_lock_count == 0

    @pytest.mark.asyncio
    async def test_precreated_task_is_reclaimed_and_never_becomes_transaction_owner(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)
        child_tasks = []
        child_views = []
        child_started = asyncio.Event()

        async def child():
            child_views.append(manager._current_transaction((1, 2)))
            child_started.set()
            await asyncio.Event().wait()

        async def return_scheduled_task(session):
            session.data["value"] = 2
            task = asyncio.create_task(child())
            child_tasks.append(task)
            await child_started.wait()
            return task

        with pytest.raises(TypeError, match="scheduled Task or Future"):
            await manager.update(1, 2, return_scheduled_task)
        assert child_tasks[0].cancelled()
        assert child_views == [None]
        assert manager._transactions == {}
        assert await manager.peek(1, 2) == original

    @pytest.mark.asyncio
    async def test_pending_cancellation_before_sync_callback_cannot_commit(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)

        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await manager.update(1, 2, lambda session: session.set("value", 2))

        assert await manager.peek(1, 2) == original
        assert manager._transactions == {}
        assert manager.active_key_lock_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("callback_kind", ["sync", "async"])
    async def test_callback_self_cancellation_is_delivered_before_commit(self, callback_kind):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        original = await manager.peek(1, 2)

        def cancel_self(session):
            session.set("value", 2)
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

        async def cancel_self_async(session):
            cancel_self(session)

        callback = cancel_self if callback_kind == "sync" else cancel_self_async
        with pytest.raises(asyncio.CancelledError):
            await manager.update(1, 2, callback)

        assert await manager.peek(1, 2) == original
        assert manager._transactions == {}
        assert manager.active_key_lock_count == 0

    @pytest.mark.asyncio
    async def test_child_task_does_not_inherit_transaction_and_waits_for_commit(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        child_tasks = []

        async def transaction(session):
            session.data["value"] = 2
            child = asyncio.create_task(manager.peek(1, 2))
            child_tasks.append(child)
            await asyncio.sleep(0)
            assert child.done() is False

        await manager.update(1, 2, transaction)
        observed = await asyncio.wait_for(child_tasks[0], timeout=1)
        assert observed is not None
        assert observed.data["value"] == 2

    @pytest.mark.asyncio
    async def test_repeated_cancellation_cleans_registry_and_key_lock(self):
        manager = SessionManager()
        await manager.create(1, 2, "test", {"value": 1})
        entered = asyncio.Event()
        cancelled_once = asyncio.Event()

        async def stubborn(session):
            session.data["value"] = 2
            entered.set()
            for _ in range(2):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled_once.set()
            return "swallowed twice"

        task = asyncio.create_task(manager.update(1, 2, stubborn))
        await entered.wait()
        task.cancel()
        await cancelled_once.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert manager._transactions == {}
        assert manager.active_key_lock_count == 0
        committed = await manager.peek(1, 2)
        assert committed is not None and committed.data["value"] == 1

    @pytest.mark.asyncio
    async def test_identity_is_normalized_and_invalid_identity_is_rejected(self):
        manager = SessionManager()
        created = await manager.create("123", "456", " plugin ")
        assert created.user_id == 123
        assert created.group_id == 456
        assert created.plugin_name == "plugin"

        with pytest.raises(TypeError):
            await manager.create(True, None, "test")
        with pytest.raises(ValueError):
            await manager.create(0, None, "test")
        with pytest.raises(ValueError):
            await manager.create(1, None, "   ")

    @pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
    def test_default_timeout_requires_a_positive_finite_number(self, invalid):
        with pytest.raises((TypeError, ValueError)):
            SessionManager(default_timeout=invalid)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
    async def test_create_timeout_requires_a_positive_finite_number(self, invalid):
        manager = SessionManager(default_timeout=10)
        with pytest.raises((TypeError, ValueError)):
            await manager.create(1, None, "test", timeout=invalid)
        assert await manager.exists(1, None) is False

    @pytest.mark.asyncio
    async def test_none_timeout_uses_default_and_zero_does_not(self):
        manager = SessionManager(default_timeout=12.5)
        created = await manager.create(1, None, "test", timeout=None)
        assert created.timeout == 12.5
        with pytest.raises(ValueError):
            await manager.create(2, None, "test", timeout=0)

    @pytest.mark.asyncio
    async def test_session_delete_value_helper(self):
        manager = SessionManager()
        await manager.create(1, None, "test", {"present": None})

        async def remove(session):
            assert session.delete("missing") is False
            assert session.delete("present") is True
            assert session.delete("present") is False

        await manager.update(1, None, remove)
        committed = await manager.peek(1, None)
        assert committed is not None
        assert "present" not in committed.data
