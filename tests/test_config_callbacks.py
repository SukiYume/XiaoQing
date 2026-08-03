"""重载回调和安全更新。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    ConfigManager,
    ConfigSnapshot,
    asyncio,
    pytest,
    threading,
)

config_file = _fixture_support.config_file
config_manager = _fixture_support.config_manager
secrets_file = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigManagerOnReload:
    """ConfigManager.on_reload 测试"""

    def test_notification_bookkeeping_has_no_unused_waiter_state(
        self,
        config_manager: ConfigManager,
    ):
        from core.config import _ConfigNotification

        assert tuple(_ConfigNotification.__dataclass_fields__) == (
            "revision",
            "snapshot",
            "callbacks",
        )
        assert not hasattr(config_manager, "_notification_thread_id")
        assert config_manager._notification_queue.maxlen == 1

    @pytest.mark.asyncio
    async def test_reload_callback(self, config_manager: ConfigManager):
        """手动 reload 不触发只属于 watch 方法的重新加载回调。"""
        callbacks_called = []

        def callback(snapshot: ConfigSnapshot):
            callbacks_called.append(snapshot)

        config_manager.on_reload(callback)
        config_manager.reload()

        # 手动 reload 不触发回调（回调只通过文件监控触发）
        assert len(callbacks_called) == 0

    def test_callbacks_share_only_a_deeply_immutable_snapshot(
        self,
        config_manager: ConfigManager,
    ):
        callback_snapshots: list[ConfigSnapshot] = []
        mutation_errors: list[BaseException] = []
        observed: list[tuple[str, tuple[int, ...], int]] = []

        def malicious(snapshot: ConfigSnapshot) -> None:
            callback_snapshots.append(snapshot)
            attacks = (
                lambda: dict.__setitem__(snapshot.config, "bot_name", "poisoned"),
                lambda: dict.update(snapshot.secrets, {"admin_user_ids": [999]}),
                lambda: list.__setitem__(snapshot.config["command_prefixes"], 0, "#"),
                lambda: object.__setattr__(snapshot, "revision", 999),
            )
            for attack in attacks:
                try:
                    attack()
                except BaseException as exc:
                    mutation_errors.append(exc)
            mutable = snapshot.mutable_secrets()
            mutable["admin_user_ids"].append(999)

        def observer(snapshot: ConfigSnapshot) -> None:
            callback_snapshots.append(snapshot)
            observed.append(
                (
                    str(snapshot.config["bot_name"]),
                    tuple(snapshot.secrets["admin_user_ids"]),
                    snapshot.revision,
                )
            )

        config_manager.on_reload(malicious)
        config_manager.on_reload(observer)
        config_manager.update_secret("admin_user_ids", [2028])

        assert len(mutation_errors) == 4
        assert all(isinstance(error, (AttributeError, TypeError)) for error in mutation_errors)
        assert callback_snapshots[0] is callback_snapshots[1]
        assert observed == [("测试机器人", (2028,), 1)]
        assert config_manager.secrets["admin_user_ids"] == (2028,)

    def test_sync_fatal_callback_does_not_wedge_later_notifications(
        self,
        config_manager: ConfigManager,
    ):
        class FatalCallback(BaseException):
            pass

        fatal_revisions: list[int] = []
        observed_revisions: list[int] = []

        def fatal(snapshot: ConfigSnapshot) -> None:
            fatal_revisions.append(snapshot.revision)
            raise FatalCallback("plugin callback escaped Exception")

        config_manager.on_reload(fatal)
        config_manager.on_reload(lambda snapshot: observed_revisions.append(snapshot.revision))

        config_manager.update_secret("admin_user_ids", [2029])
        config_manager.update_secret("admin_user_ids", [2030])

        assert fatal_revisions == observed_revisions
        assert len(observed_revisions) == 2
        assert observed_revisions == sorted(observed_revisions)
        assert config_manager.last_notified_revision == observed_revisions[-1]

    @pytest.mark.asyncio
    async def test_async_fatal_callback_does_not_wedge_later_notifications(
        self,
        config_manager: ConfigManager,
    ):
        class FatalCallback(BaseException):
            pass

        fatal_revisions: list[int] = []
        observed: asyncio.Queue[int] = asyncio.Queue()

        async def fatal(snapshot: ConfigSnapshot) -> None:
            await asyncio.sleep(0)
            fatal_revisions.append(snapshot.revision)
            raise FatalCallback("async plugin callback escaped Exception")

        async def observer(snapshot: ConfigSnapshot) -> None:
            await asyncio.sleep(0)
            observed.put_nowait(snapshot.revision)

        config_manager.on_reload(fatal)
        config_manager.on_reload(observer)

        config_manager.update_secret("admin_user_ids", [2031])
        first = await asyncio.wait_for(observed.get(), timeout=2)
        config_manager.update_secret("admin_user_ids", [2032])
        second = await asyncio.wait_for(observed.get(), timeout=2)

        assert fatal_revisions == [first, second]
        assert first < second
        assert config_manager.last_notified_revision == second

    @pytest.mark.asyncio
    async def test_async_callback_to_thread_mutation_does_not_deadlock(
        self,
        config_manager: ConfigManager,
    ):
        revisions: list[int] = []
        completed = asyncio.Event()

        async def callback(snapshot: ConfigSnapshot) -> None:
            revisions.append(snapshot.revision)
            if len(revisions) == 1:
                await asyncio.to_thread(
                    config_manager.set_plugin_secret,
                    "qingssh",
                    "callback_thread",
                    "committed",
                )
            elif len(revisions) == 2:
                completed.set()

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [2033])

        await asyncio.wait_for(completed.wait(), timeout=2)
        assert revisions == sorted(revisions)
        assert len(revisions) == 2
        assert config_manager.last_notified_revision == revisions[-1]
        assert config_manager.get_plugin_secret("qingssh", "callback_thread") == "committed"

    @pytest.mark.asyncio
    async def test_hanging_async_callback_times_out_without_blocking_next_subscriber(
        self,
        config_manager: ConfigManager,
    ) -> None:
        entered = asyncio.Event()
        observed = asyncio.Event()
        observed_revisions: list[int] = []
        config_manager._callback_timeout_seconds = 0.02

        async def hanging(_snapshot: ConfigSnapshot) -> None:
            entered.set()
            await asyncio.Future()

        async def observer(snapshot: ConfigSnapshot) -> None:
            observed_revisions.append(snapshot.revision)
            observed.set()

        config_manager.on_reload(hanging)
        config_manager.on_reload(observer)
        config_manager.update_secret("admin_user_ids", [2040])

        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert observed_revisions == [config_manager.revision]
        assert config_manager.last_notified_revision == config_manager.revision

    @pytest.mark.asyncio
    async def test_pending_config_notifications_coalesce_to_latest_revision(
        self,
        config_manager: ConfigManager,
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()
        observed_revisions: list[int] = []
        config_manager._callback_timeout_seconds = 1.0

        async def callback(snapshot: ConfigSnapshot) -> None:
            observed_revisions.append(snapshot.revision)
            if len(observed_revisions) == 1:
                entered.set()
                await release.wait()
            else:
                completed.set()

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [2041])
        await asyncio.wait_for(entered.wait(), timeout=1)
        first_revision = observed_revisions[0]

        for user_id in range(2042, 2052):
            config_manager.update_secret("admin_user_ids", [user_id])
        latest_revision = config_manager.revision

        assert len(config_manager._notification_queue) == 1
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=1)

        assert observed_revisions == [first_revision, latest_revision]
        assert config_manager.last_notified_revision == latest_revision


class TestConfigManagerSecurityUpdates:
    def test_security_updates_are_synchronous_isolated_and_contained(
        self,
        config_manager: ConfigManager,
    ):
        class FatalSecurityCallback(BaseException):
            pass

        fatal_revisions: list[int] = []
        security_revisions: list[int] = []
        ordinary_entered = threading.Event()
        release_ordinary = threading.Event()
        mutation_returned = threading.Event()

        def fatal(snapshot: ConfigSnapshot) -> None:
            fatal_revisions.append(snapshot.revision)
            raise FatalSecurityCallback("authorization hook failed")

        def security(snapshot: ConfigSnapshot) -> None:
            security_revisions.append(snapshot.revision)

        def slow_ordinary(_snapshot: ConfigSnapshot) -> None:
            ordinary_entered.set()
            assert release_ordinary.wait(timeout=2)

        config_manager.on_security_update(fatal)
        unsubscribe_security = config_manager.on_security_update(security)
        unsubscribe_ordinary = config_manager.on_reload(slow_ordinary)

        def mutate() -> None:
            config_manager.update_secret("admin_user_ids", [2034])
            mutation_returned.set()

        writer = threading.Thread(target=mutate)
        writer.start()
        assert ordinary_entered.wait(timeout=1)
        assert len(fatal_revisions) == 1
        assert security_revisions == fatal_revisions
        assert not mutation_returned.is_set()

        unsubscribe_security()
        unsubscribe_security()
        unsubscribe_ordinary()
        release_ordinary.set()
        writer.join(timeout=2)
        assert not writer.is_alive()
        assert mutation_returned.is_set()

        config_manager.update_secret("admin_user_ids", [2035])
        assert len(fatal_revisions) == 2
        assert len(security_revisions) == 1
        assert config_manager.secrets["admin_user_ids"] == (2035,)
