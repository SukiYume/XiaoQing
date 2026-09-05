"""线程安全和插件密钥存储。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    Any,
    ConfigManager,
    ConfigSnapshot,
    Path,
    _last_notified_revision,
    asyncio,
    json,
    pytest,
    threading,
    time,
)

config_file     = _fixture_support.config_file
config_manager  = _fixture_support.config_manager
secrets_file    = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigManagerThreadSafety:
    """ConfigManager 线程安全测试"""

    def test_concurrent_reads(self, config_manager: ConfigManager):
        """测试并发读取"""
        results = []

        def read_config():
            for _ in range(100):
                config = config_manager.config
                results.append(config.get("bot_name"))

        threads = [threading.Thread(target=read_config) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 1000
        assert all(r == "测试机器人" for r in results)

    def test_concurrent_set_delete_and_update_have_no_lost_updates(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        writers = [
            threading.Thread(
                target = config_manager.set_plugin_secret,
                args   = ("qingssh", f"passwords.ref-{index}", f"secret-{index}"),
            )
            for index in range(20)
        ]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=3)

        mutations = [
            threading.Thread(
                target = config_manager.delete_plugin_secret,
                args   = ("qingssh", f"passwords.ref-{index}"),
            )
            for index in range(0, 20, 2)
        ]
        mutations.append(
            threading.Thread(
                target = config_manager.update_secret,
                args   = ("admin_user_ids", [8080]),
            )
        )
        for mutation in mutations:
            mutation.start()
        for mutation in mutations:
            mutation.join(timeout=3)

        assert all(not thread.is_alive() for thread in writers + mutations)
        for index in range(20):
            expected = None if index % 2 == 0 else f"secret-{index}"
            assert (
                config_manager.get_plugin_secret(
                    "qingssh",
                    f"passwords.ref-{index}",
                )
                == expected
            )
        assert config_manager.secrets["admin_user_ids"] == (8080,)
        assert (
            json.loads(secrets_file.read_text(encoding="utf-8"))
            == config_manager.snapshot().mutable_secrets()
        )

    def test_delayed_first_writer_cannot_overwrite_later_secret_update(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from core import config as config_module

        real_write                  = config_module._write_secret_payload
        first_write_entered         = threading.Event()
        release_first_write         = threading.Event()
        write_calls                 = 0
        call_lock                   = threading.Lock()
        errors: list[BaseException] = []

        def delayed_write(handle: Any, payload: bytes) -> None:
            nonlocal write_calls
            with call_lock:
                write_calls += 1
                call = write_calls
            if call == 1:
                first_write_entered.set()
                assert release_first_write.wait(timeout=2)
            real_write(handle, payload)

        def run(operation) -> None:
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)

        monkeypatch.setattr(config_module, "_write_secret_payload", delayed_write)
        first = threading.Thread(
            target = run,
            args   = (lambda: config_manager.update_secret("admin_user_ids", [111]),),
        )
        second = threading.Thread(
            target = run,
            args   = (lambda: config_manager.set_plugin_secret("qingssh", "passwords.b", "two"),),
        )
        first.start()
        assert first_write_entered.wait(timeout=1)
        second.start()
        time.sleep(0.03)
        assert write_calls == 1

        release_first_write.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert errors == []
        assert not first.is_alive() and not second.is_alive()
        disk = json.loads(secrets_file.read_text(encoding="utf-8"))
        assert config_manager.secrets["admin_user_ids"] == (111,)
        assert config_manager.secrets["plugins"]["qingssh"]["passwords"]["b"] == "two"
        assert disk == config_manager.snapshot().mutable_secrets()

    def test_failed_writer_never_publishes_or_rolls_back_successor(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from core import config as config_module

        real_write                    = config_module._write_secret_payload
        failing_write_entered         = threading.Event()
        release_failure               = threading.Event()
        failures: list[BaseException] = []

        def fail_selected(handle: Any, payload: bytes) -> None:
            if json.loads(payload.decode("utf-8")).get("admin_user_ids") == [999]:
                failing_write_entered.set()
                assert release_failure.wait(timeout=2)
                raise OSError("disk full")
            real_write(handle, payload)

        def failing_writer() -> None:
            try:
                config_manager.update_secret("admin_user_ids", [999])
            except BaseException as exc:
                failures.append(exc)

        monkeypatch.setattr(config_module, "_write_secret_payload", fail_selected)
        first = threading.Thread(target=failing_writer)
        second = threading.Thread(
            target=lambda: config_manager.set_plugin_secret("qingssh", "passwords.ok", "saved")
        )
        first.start()
        assert failing_write_entered.wait(timeout=1)
        second.start()
        release_failure.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert len(failures) == 1 and isinstance(failures[0], OSError)
        assert config_manager.secrets["admin_user_ids"] == (12345, 67890)
        assert config_manager.get_plugin_secret("qingssh", "passwords.ok") == "saved"
        assert (
            json.loads(secrets_file.read_text(encoding="utf-8"))
            == config_manager.snapshot().mutable_secrets()
        )

    def test_reload_read_publish_is_serialized_with_secret_writer(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_read          = config_manager._read_source_unlocked
        reload_reading_secrets = threading.Event()
        release_reload         = threading.Event()

        def delayed_read(path: Path):
            if path == secrets_file and not reload_reading_secrets.is_set():
                reload_reading_secrets.set()
                assert release_reload.wait(timeout=2)
            return original_read(path)

        monkeypatch.setattr(config_manager, "_read_source_unlocked", delayed_read)
        reload_thread = threading.Thread(target=config_manager.reload)
        writer_thread = threading.Thread(
            target=lambda: config_manager.set_plugin_secret("qingssh", "after_reload", "kept")
        )
        reload_thread.start()
        assert reload_reading_secrets.wait(timeout=1)
        writer_thread.start()
        time.sleep(0.03)
        assert writer_thread.is_alive()

        release_reload.set()
        reload_thread.join(timeout=2)
        writer_thread.join(timeout=2)

        assert not reload_thread.is_alive() and not writer_thread.is_alive()
        assert config_manager.get_plugin_secret("qingssh", "after_reload") == "kept"
        assert (
            json.loads(secrets_file.read_text(encoding="utf-8"))
            == config_manager.snapshot().mutable_secrets()
        )

    def test_callbacks_are_revision_ordered_across_writers(
        self,
        config_manager: ConfigManager,
    ):
        first_callback_entered = threading.Event()
        release_first_callback = threading.Event()
        revisions: list[int]   = []

        def callback(snapshot: ConfigSnapshot) -> None:
            revisions.append(snapshot.revision)
            if len(revisions) == 1:
                first_callback_entered.set()
                assert release_first_callback.wait(timeout=2)

        config_manager.on_reload(callback)
        first = threading.Thread(
            target=lambda: config_manager.update_secret("admin_user_ids", [101])
        )
        second = threading.Thread(
            target=lambda: config_manager.set_plugin_secret("qingssh", "ordered", True)
        )
        first.start()
        assert first_callback_entered.wait(timeout=1)
        second.start()
        second.join(timeout=1)
        assert not second.is_alive()

        release_first_callback.set()
        first.join(timeout=2)

        assert revisions == sorted(revisions)
        assert len(revisions) == 2
        assert _last_notified_revision(config_manager) == revisions[-1]

    def test_callback_can_join_raw_mutation_thread_without_deadlock(
        self,
        config_manager: ConfigManager,
    ):
        revisions: list[int]               = []
        nested_errors: list[BaseException] = []

        def callback(snapshot: ConfigSnapshot) -> None:
            revisions.append(snapshot.revision)
            if len(revisions) != 1:
                return

            def mutate() -> None:
                try:
                    config_manager.set_plugin_secret("qingssh", "raw_thread", "committed")
                except BaseException as exc:
                    nested_errors.append(exc)

            nested_writer = threading.Thread(target=mutate)
            nested_writer.start()
            nested_writer.join(timeout=1)
            assert not nested_writer.is_alive()

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [304])

        assert nested_errors == []
        assert revisions == sorted(revisions)
        assert len(revisions) == 2
        assert _last_notified_revision(config_manager) == revisions[-1]
        assert config_manager.get_plugin_secret("qingssh", "raw_thread") == "committed"

    def test_callback_reentry_queues_next_revision_without_recursion(
        self,
        config_manager: ConfigManager,
    ):
        revisions: list[int] = []
        depth                = 0
        maximum_depth        = 0
        reentered            = False

        def callback(snapshot: ConfigSnapshot) -> None:
            nonlocal depth, maximum_depth, reentered
            depth += 1
            maximum_depth = max(maximum_depth, depth)
            revisions.append(snapshot.revision)
            if not reentered:
                reentered = True
                config_manager.set_plugin_secret("qingssh", "from_callback", "ok")
            depth -= 1

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [303])

        assert len(revisions) == 2
        assert revisions == sorted(revisions)
        assert maximum_depth == 1
        assert config_manager.get_plugin_secret("qingssh", "from_callback") == "ok"

    @pytest.mark.asyncio
    async def test_async_callbacks_run_on_bound_loop_in_revision_order(
        self,
        config_manager: ConfigManager,
    ):
        loop_thread                 = threading.get_ident()
        seen: list[tuple[int, int]] = []
        delivered                   = asyncio.Event()

        async def callback(snapshot: ConfigSnapshot) -> None:
            await asyncio.sleep(0)
            seen.append((snapshot.revision, threading.get_ident()))
            if len(seen) == 2:
                delivered.set()

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [401])
        first_revision = config_manager.revision
        config_manager.set_plugin_secret("qingssh", "async_order", True)
        latest_revision = config_manager.revision
        assert len(config_manager._notification_queue) == 1
        await asyncio.wait_for(delivered.wait(), timeout=1)

        assert [revision for revision, _thread in seen] == [first_revision, latest_revision]
        assert all(thread_id == loop_thread for _revision, thread_id in seen)


def test_plugin_secret_store_creates_reads_and_deletes_scoped_values(config_manager: ConfigManager):
    config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "top-secret")

    assert config_manager.get_plugin_secret("qingssh", "passwords.ref-1") == "top-secret"
    assert config_manager.get_plugin_secret("other", "passwords.ref-1") is None
    assert config_manager.delete_plugin_secret("qingssh", "passwords.ref-1") is True
    assert config_manager.get_plugin_secret("qingssh", "passwords.ref-1") is None
