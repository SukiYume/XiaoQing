"""配置文件监听和稳定读取。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    Any,
    ConfigManager,
    ConfigSnapshot,
    ConfigSourceStatus,
    Path,
    asyncio,
    json,
    os,
    pytest,
    threading,
    time,
)

config_file = _fixture_support.config_file
config_manager = _fixture_support.config_manager
secrets_file = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigManagerWatch:
    """ConfigManager.watch 测试"""

    @pytest.mark.asyncio
    async def test_watch_detects_changes(self, config_manager: ConfigManager, config_file: Path):
        """测试监控文件变化"""
        changes_detected = []
        changed = asyncio.Event()

        def callback(snapshot: ConfigSnapshot):
            changes_detected.append(snapshot)
            changed.set()

        config_manager.on_reload(callback)

        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        await asyncio.sleep(0)

        # 修改文件
        new_data = {"bot_name": "changed"}
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(new_data, f)

        try:
            await asyncio.wait_for(changed.wait(), timeout=2)
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

        assert len(changes_detected) == 1
        assert changes_detected[0].config["bot_name"] == "changed"

    @pytest.mark.asyncio
    async def test_watch_detects_change_written_during_callback(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        """测试回调执行期间写入的新配置不会被 mtime 覆盖掉"""
        changes_detected = []
        both_changes = asyncio.Event()
        first_mtime = time.time() + 10
        second_mtime = first_mtime + 10

        def write_config(bot_name: str, mtime: float) -> None:
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump({"bot_name": bot_name}, f)
            os.utime(config_file, (mtime, mtime))

        def callback(snapshot: ConfigSnapshot):
            changes_detected.append(snapshot.config.get("bot_name"))
            if len(changes_detected) == 1:
                write_config("second", second_mtime)
            elif len(changes_detected) == 2:
                both_changes.set()

        config_manager.on_reload(callback)
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        await asyncio.sleep(0)

        write_config("first", first_mtime)

        try:
            await asyncio.wait_for(both_changes.wait(), timeout=2)
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

        assert changes_detected[:2] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_watch_detects_edit_between_manager_creation_and_watch_start(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        changed = asyncio.Event()
        snapshots: list[ConfigSnapshot] = []

        def callback(snapshot: ConfigSnapshot) -> None:
            snapshots.append(snapshot)
            changed.set()

        config_manager.on_reload(callback)
        config_file.write_text('{"bot_name":"startup-gap"}', encoding="utf-8")
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        try:
            await asyncio.wait_for(changed.wait(), timeout=2)
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

        assert snapshots[0].config["bot_name"] == "startup-gap"
        assert snapshots[0].config_status is ConfigSourceStatus.VALID

    @pytest.mark.asyncio
    async def test_watch_running_secret_deletion_fails_closed(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        changed = asyncio.Event()
        snapshots: list[ConfigSnapshot] = []

        def callback(snapshot: ConfigSnapshot) -> None:
            snapshots.append(snapshot)
            changed.set()

        config_manager.on_reload(callback)
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        await asyncio.sleep(0)
        secrets_file.unlink()
        try:
            await asyncio.wait_for(changed.wait(), timeout=2)
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

        assert snapshots[0].secrets_status is ConfigSourceStatus.MISSING
        assert snapshots[0].secrets == {}
        assert config_manager.secrets == {}

    @pytest.mark.asyncio
    async def test_watch_detects_same_size_content_change_with_preserved_mtime(
        self,
        temp_config_dir: Path,
    ):
        config_path = temp_config_dir / "same-mtime-config.json"
        secrets_path = temp_config_dir / "same-mtime-secrets.json"
        config_path.write_text('{"bot_name":"AAAA"}', encoding="utf-8")
        secrets_path.write_text("{}", encoding="utf-8")
        manager = ConfigManager(config_path, secrets_path)
        original_stat = config_path.stat()
        changed = asyncio.Event()
        snapshots: list[ConfigSnapshot] = []

        def callback(snapshot: ConfigSnapshot) -> None:
            snapshots.append(snapshot)
            changed.set()

        manager.on_reload(callback)
        watch_task = asyncio.create_task(manager.watch(interval=0.01))
        await asyncio.sleep(0)
        config_path.write_text('{"bot_name":"BBBB"}', encoding="utf-8")
        os.utime(
            config_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        assert config_path.stat().st_mtime_ns == original_stat.st_mtime_ns
        try:
            await asyncio.wait_for(changed.wait(), timeout=2)
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

        assert snapshots[0].config["bot_name"] == "BBBB"

    @pytest.mark.asyncio
    async def test_unchanged_watch_reuses_stat_confirmations_and_parsed_payloads(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import core.config as config_module

        full_reads: list[Path] = []
        validation_calls = 0
        original_read = config_manager._read_source_unlocked
        original_validate = config_module._validate_runtime_config

        def record_read(path: Path):
            full_reads.append(path)
            return original_read(path)

        def record_validation(value):
            nonlocal validation_calls
            validation_calls += 1
            return original_validate(value)

        monkeypatch.setattr(config_manager, "_read_source_unlocked", record_read)
        monkeypatch.setattr(config_module, "_validate_runtime_config", record_validation)

        await config_manager._watch_reconcile_once()

        assert full_reads == [config_file, secrets_file]
        assert validation_calls == 0

    def test_unchanged_explicit_reload_reuses_stat_confirmations_and_parsed_payloads(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import core.config as config_module

        full_reads: list[Path] = []
        validation_calls = 0
        original_read = config_manager._read_source_unlocked
        original_validate = config_module._validate_runtime_config

        def record_read(path: Path):
            full_reads.append(path)
            return original_read(path)

        def record_validation(value):
            nonlocal validation_calls
            validation_calls += 1
            return original_validate(value)

        monkeypatch.setattr(config_manager, "_read_source_unlocked", record_read)
        monkeypatch.setattr(config_module, "_validate_runtime_config", record_validation)

        config_manager.reload()

        assert full_reads == [config_file, secrets_file]
        assert validation_calls == 0

    @pytest.mark.asyncio
    async def test_watch_cancellation_during_worker_read_never_publishes_late(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_read_sources = config_manager._read_sources
        worker_entered = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()

        def blocked_read_sources(*args, **kwargs):
            worker_entered.set()
            assert release_worker.wait(timeout=3)
            try:
                return original_read_sources(*args, **kwargs)
            finally:
                worker_finished.set()

        monkeypatch.setattr(config_manager, "_read_sources", blocked_read_sources)
        before = config_manager.snapshot()
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        assert await asyncio.to_thread(worker_entered.wait, 2)
        config_file.write_text('{"bot_name":"must-not-publish"}', encoding="utf-8")

        watch_task.cancel()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await watch_task
        assert await asyncio.to_thread(worker_finished.wait, 2)
        await asyncio.sleep(0)

        after = config_manager.snapshot()
        assert after.revision == before.revision
        assert after.mutable_config() == before.mutable_config()

    @pytest.mark.asyncio
    async def test_every_watch_stability_read_runs_off_the_event_loop(
        self,
        config_manager: ConfigManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_read_sources = config_manager._read_sources
        second_read_entered = threading.Event()
        release_second_read = threading.Event()
        calls = 0

        def block_second_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                second_read_entered.set()
                assert release_second_read.wait(timeout=2)
            return original_read_sources(*args, **kwargs)

        monkeypatch.setattr(config_manager, "_read_sources", block_second_read)
        watchdog = threading.Timer(1.0, release_second_read.set)
        watchdog.start()
        started = asyncio.get_running_loop().time()
        reconcile = asyncio.create_task(config_manager._watch_reconcile_once())
        try:
            assert await asyncio.to_thread(second_read_entered.wait, 2)
            event_loop_delay = asyncio.get_running_loop().time() - started
            release_second_read.set()
            await reconcile
        finally:
            release_second_read.set()
            watchdog.cancel()

        assert calls == 3
        assert event_loop_delay < 0.5

    @pytest.mark.asyncio
    async def test_watch_invalid_secrets_then_fixed_source_recovers(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        snapshots: asyncio.Queue[ConfigSnapshot] = asyncio.Queue()
        config_manager.on_reload(snapshots.put_nowait)
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        await asyncio.sleep(0)
        secrets_file.write_bytes(b"\xff\xfe")

        try:
            invalid = await asyncio.wait_for(snapshots.get(), timeout=2)
            assert invalid.secrets_status is ConfigSourceStatus.INVALID
            assert invalid.secrets == {}
            assert not watch_task.done()

            recovered_payload = {
                "admin_user_ids": [],
                "plugins": {"echo": {"api_key": "recovered"}},
            }
            secrets_file.write_text(json.dumps(recovered_payload), encoding="utf-8")
            recovered = await asyncio.wait_for(snapshots.get(), timeout=2)
            assert recovered.secrets_status is ConfigSourceStatus.INCONSISTENT
            assert recovered.secrets == {}
            assert recovered.revision == invalid.revision + 1
            assert not watch_task.done()

            confirmed = config_manager.reload()
            assert confirmed.secrets_status is ConfigSourceStatus.VALID
            assert confirmed.secrets["plugins"]["echo"]["api_key"] == "recovered"
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

    @pytest.mark.asyncio
    async def test_watch_unexpected_read_failure_failcloses_and_continues(
        self,
        config_manager: ConfigManager,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_read_sources = config_manager._read_sources
        calls = 0
        snapshots: asyncio.Queue[ConfigSnapshot] = asyncio.Queue()

        def flaky_read_sources(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("unexpected reader failure")
            return original_read_sources(*args, **kwargs)

        monkeypatch.setattr(config_manager, "_read_sources", flaky_read_sources)
        config_manager.on_reload(snapshots.put_nowait)
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        try:
            failed = await asyncio.wait_for(snapshots.get(), timeout=2)
            assert failed.secrets_status is ConfigSourceStatus.UNAVAILABLE
            assert failed.secrets == {}
            assert not watch_task.done()

            recovered = await asyncio.wait_for(snapshots.get(), timeout=2)
            assert recovered.secrets_status is ConfigSourceStatus.VALID
            assert recovered.secrets["plugins"]["echo"]["api_key"] == "test_key"
            assert recovered.revision == failed.revision + 1
            assert not watch_task.done()
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

    @pytest.mark.asyncio
    async def test_watch_never_publishes_secret_deleted_before_final_stable_read(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_read_sources = config_manager._read_sources
        rotated = {
            "admin_user_ids": [],
            "plugins": {"echo": {"api_key": "must-never-authorize"}},
        }
        calls = 0
        snapshots: list[ConfigSnapshot] = []
        missing = asyncio.Event()

        def create_then_delete_before_final_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                secrets_file.write_text(json.dumps(rotated), encoding="utf-8")
            elif calls == 3:
                secrets_file.unlink()
            return original_read_sources(*args, **kwargs)

        def observe(snapshot: ConfigSnapshot) -> None:
            snapshots.append(snapshot)
            if snapshot.secrets_status is ConfigSourceStatus.MISSING:
                missing.set()

        monkeypatch.setattr(
            config_manager,
            "_read_sources",
            create_then_delete_before_final_read,
        )
        config_manager.on_reload(observe)
        watch_task = asyncio.create_task(config_manager.watch(interval=0.01))
        try:
            await asyncio.wait_for(missing.wait(), timeout=2)
        finally:
            watch_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watch_task

        assert calls >= 6
        assert not secrets_file.exists()
        assert config_manager.snapshot().secrets_status is ConfigSourceStatus.MISSING
        assert config_manager.secrets == {}
        assert all(
            snapshot.secrets_status is not ConfigSourceStatus.VALID
            or snapshot.secrets.get("plugins", {}).get("echo", {}).get("api_key")
            != "must-never-authorize"
            for snapshot in snapshots
        )

    @pytest.mark.asyncio
    async def test_reload_linearization_boundary_is_revoked_by_next_reconcile(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        original_read_sources = config_manager._read_sources
        read_calls = 0
        security_publications: list[ConfigSnapshot] = []

        def unlink_after_final_stable_read(*args, **kwargs):
            nonlocal read_calls
            sources = original_read_sources(*args, **kwargs)
            read_calls += 1
            if read_calls == 3:
                # No finite sequence of rereads can eliminate an uncooperative
                # external write immediately after the last read.  The third
                # stable read is reload()'s linearization point; this deliberately
                # does not claim cross-process atomicity.
                secrets_file.unlink()
            return sources

        config_manager.on_security_update(security_publications.append)
        monkeypatch.setattr(
            config_manager,
            "_read_sources",
            unlink_after_final_stable_read,
        )

        confirmed_at_linearization_point = config_manager.reload()

        assert read_calls == 3
        assert not secrets_file.exists()
        assert confirmed_at_linearization_point.secrets_status is ConfigSourceStatus.VALID
        assert confirmed_at_linearization_point.secrets["plugins"]["echo"]["api_key"] == "test_key"
        assert security_publications[-1] is confirmed_at_linearization_point

        monkeypatch.setattr(config_manager, "_read_sources", original_read_sources)
        await config_manager._watch_reconcile_once()

        revoked = config_manager.snapshot()
        assert revoked.revision == confirmed_at_linearization_point.revision + 1
        assert revoked.secrets_status is ConfigSourceStatus.MISSING
        assert revoked.secrets == {}
        assert security_publications[-1].secrets_status is ConfigSourceStatus.MISSING
        assert security_publications[-1].secrets == {}

    @pytest.mark.asyncio
    async def test_managed_secret_commit_remains_paired_on_next_reconcile(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        config_manager.set_plugin_secret("qingssh", "managed", "mutation")

        committed = config_manager.snapshot()
        await config_manager._watch_reconcile_once()
        reconciled = config_manager.snapshot()

        assert reconciled.revision == committed.revision
        assert reconciled.secrets_status is ConfigSourceStatus.VALID
        assert reconciled.mutable_secrets() == committed.mutable_secrets()
        assert reconciled.mutable_secrets() == json.loads(secrets_file.read_text(encoding="utf-8"))

    @pytest.mark.asyncio
    async def test_identical_external_atomic_replacement_requires_reload(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        original = config_manager.snapshot()
        original_identity = config_manager._secrets_source.identity
        replacement = secrets_file.with_name("replacement-secrets.json")
        replacement.write_bytes(secrets_file.read_bytes())
        os.replace(replacement, secrets_file)

        await config_manager._watch_reconcile_once()

        pending = config_manager.snapshot()
        assert config_manager._secrets_source.identity != original_identity
        assert pending.revision == original.revision + 1
        assert pending.secrets_status is ConfigSourceStatus.INCONSISTENT
        assert pending.secrets == {}

        confirmed = config_manager.reload()
        assert confirmed.secrets_status is ConfigSourceStatus.VALID
        assert confirmed.mutable_secrets() == original.mutable_secrets()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "external_steps",
        [("config",), ("secrets",), ("config", "secrets")],
        ids=["config-only", "secrets-only", "staged-pair"],
    )
    async def test_external_file_staging_never_authorizes_until_reload(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
        external_steps: tuple[str, ...],
    ):
        rotated = {
            "admin_user_ids": [9090],
            "plugins": {"echo": {"api_key": "explicitly-confirmed-only"}},
        }
        security_publications: list[ConfigSnapshot] = []
        config_manager.on_security_update(security_publications.append)

        for index, step in enumerate(external_steps, start=1):
            if step == "config":
                config_file.write_text(
                    json.dumps({"bot_name": f"external-{index}"}),
                    encoding="utf-8",
                )
            else:
                secrets_file.write_text(json.dumps(rotated), encoding="utf-8")

            await config_manager._watch_reconcile_once()
            pending = config_manager.snapshot()
            assert pending.secrets_status is ConfigSourceStatus.INCONSISTENT
            assert pending.secrets == {}
            assert all(snapshot.secrets == {} for snapshot in security_publications)

        confirmed = config_manager.reload()
        assert confirmed.secrets_status is ConfigSourceStatus.VALID
        if "secrets" in external_steps:
            assert confirmed.mutable_secrets() == rotated
        else:
            assert confirmed.secrets["plugins"]["echo"]["api_key"] == "test_key"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "interval",
        [0, -1, True, float("nan"), float("inf"), float("-inf")],
    )
    async def test_watch_rejects_non_positive_or_boolean_interval(
        self,
        config_manager: ConfigManager,
        interval: Any,
    ):
        with pytest.raises(ValueError, match="interval must be positive"):
            await config_manager.watch(interval=interval)
