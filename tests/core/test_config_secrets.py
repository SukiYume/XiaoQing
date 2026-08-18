"""密钥更新、保存和事务。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    _MAX_CONFIG_SOURCE_BYTES,
    _MAX_CONFIG_TREE_DEPTH,
    _MAX_CONFIG_TREE_NODES,
    Any,
    ConfigLoadError,
    ConfigManager,
    ConfigSnapshot,
    ConfigSourceStatus,
    Path,
    json,
    os,
    pytest,
)

config_file = _fixture_support.config_file
config_manager = _fixture_support.config_manager
secrets_file = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigManagerUpdateSecret:
    """ConfigManager.update_secret 测试"""

    def test_update_existing_value(self, config_manager: ConfigManager, secrets_file: Path):
        """测试更新已存在的值"""
        config_manager.update_secret("admin_user_ids", [11111, 22222])

        assert config_manager.secrets["admin_user_ids"] == (11111, 22222)

    def test_update_nested_value(self, config_manager: ConfigManager):
        """测试更新嵌套值"""
        config_manager.update_secret("plugins.echo.api_key", "new_key")

        assert config_manager.secrets["plugins"]["echo"]["api_key"] == "new_key"

    def test_update_saves_to_file(self, config_manager: ConfigManager, secrets_file: Path):
        """测试更新后保存到文件"""
        config_manager.update_secret("admin_user_ids", [55555])

        # 重新读取文件验证
        with open(secrets_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["admin_user_ids"] == [55555]

    def test_update_nonexistent_key_raises(self, config_manager: ConfigManager):
        """测试更新不存在的键抛出 KeyError"""
        with pytest.raises(KeyError, match="路径不存在"):
            config_manager.update_secret("nonexistent.key", "value")

    def test_update_nonexistent_path_raises(self, config_manager: ConfigManager):
        """测试更新不存在的路径抛出 KeyError"""
        with pytest.raises(KeyError, match="路径不存在"):
            config_manager.update_secret("nonexistent.nested.key", "value")

    def test_update_non_dict_value_raises(self, config_manager: ConfigManager):
        """测试更新非字典类型的路径抛出 ValueError"""
        # admin_user_ids 是列表，不是字典
        with pytest.raises(ValueError, match="不是字典类型"):
            config_manager.update_secret("admin_user_ids.key", "value")

    def test_update_secret_triggers_reload_callbacks(self, config_manager: ConfigManager):
        """测试 update_secret 会触发 reload 回调"""
        snapshots: list[ConfigSnapshot] = []

        def callback(snapshot: ConfigSnapshot):
            snapshots.append(snapshot)

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [2024])

        assert len(snapshots) == 1
        assert snapshots[0].secrets["admin_user_ids"] == (2024,)

    def test_reload_subscription_can_be_removed_idempotently(
        self,
        config_manager: ConfigManager,
    ):
        snapshots: list[ConfigSnapshot] = []
        unsubscribe = config_manager.on_reload(snapshots.append)

        config_manager.update_secret("admin_user_ids", [2024])
        unsubscribe()
        unsubscribe()
        config_manager.update_secret("admin_user_ids", [2025])

        assert [snapshot.secrets["admin_user_ids"] for snapshot in snapshots] == [(2024,)]

    def test_reload_callback_can_unsubscribe_itself_during_notification(
        self,
        config_manager: ConfigManager,
    ):
        seen: list[list[int]] = []
        unsubscribe = None

        def callback(snapshot: ConfigSnapshot) -> None:
            seen.append(list(snapshot.secrets["admin_user_ids"]))
            assert unsubscribe is not None
            unsubscribe()

        unsubscribe = config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [2026])
        config_manager.update_secret("admin_user_ids", [2027])

        assert seen == [[2026]]

    def test_update_secret_runs_async_reload_callbacks(self, config_manager: ConfigManager):
        """测试 sync 路径下 update_secret 仍会执行 async reload 回调"""
        snapshots: list[ConfigSnapshot] = []

        async def callback(snapshot: ConfigSnapshot):
            snapshots.append(snapshot)

        config_manager.on_reload(callback)
        config_manager.update_secret("admin_user_ids", [2025])

        assert len(snapshots) == 1
        assert snapshots[0].secrets["admin_user_ids"] == (2025,)

    def test_update_secret_rolls_back_when_save_fails(
        self,
        config_manager: ConfigManager,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """候选落盘失败时 live state 从未发布。"""
        original = config_manager.secrets

        def _boom(_handle: Any, _payload: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("core.config._write_secret_payload", _boom)

        with pytest.raises(OSError, match="disk full"):
            config_manager.update_secret("admin_user_ids", [99999])

        assert config_manager.secrets == original


class TestConfigManagerSecretTransactions:
    """Content-CAS, revocation, retry, and canonicalization regressions."""

    def test_deleted_primary_refuses_mutation_and_is_not_resurrected(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        notifications: list[ConfigSnapshot] = []
        config_manager.on_reload(notifications.append)
        secrets_file.unlink()

        with pytest.raises(ConfigLoadError, match="primary source is missing"):
            config_manager.set_plugin_secret("qingssh", "passwords.new", "must-not-return")

        assert not secrets_file.exists()
        assert config_manager.secrets == {}
        assert config_manager.get_plugin_secret("echo", "api_key") is None
        assert config_manager.snapshot().secrets_status is ConfigSourceStatus.MISSING
        assert [item.secrets_status for item in notifications] == [ConfigSourceStatus.MISSING]

    def test_external_valid_change_requires_reload_then_retry_merges_without_overwrite(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        external = {
            "admin_user_ids": [],
            "plugins": {
                "echo": {"api_key": "rotated"},
                "external": {"preserve": True},
            },
        }
        original = config_manager.snapshot()
        secrets_file.write_text(json.dumps(external), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="changed on disk"):
            config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "new")

        assert json.loads(secrets_file.read_text(encoding="utf-8")) == external
        assert config_manager.snapshot().secrets_status is ConfigSourceStatus.VALID
        assert config_manager.snapshot().mutable_secrets() == original.mutable_secrets()
        assert config_manager._pending_secrets_source is not None
        assert config_manager._pending_secrets_source.value == external

        confirmed = config_manager.reload()
        assert confirmed.secrets_status is ConfigSourceStatus.VALID
        assert confirmed.mutable_secrets() == external
        config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "new")

        disk = json.loads(secrets_file.read_text(encoding="utf-8"))
        assert disk["plugins"]["echo"]["api_key"] == "rotated"
        assert disk["plugins"]["external"]["preserve"] is True
        assert disk["plugins"]["qingssh"]["passwords"]["ref-1"] == "new"
        assert config_manager.snapshot().mutable_secrets() == disk

    def test_external_valid_removal_stays_pending_before_update_reports_conflict(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        revoked = {"admin_user_ids": [], "plugins": {"echo": {}}}
        original = config_manager.snapshot()
        secrets_file.write_text(json.dumps(revoked), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="changed on disk"):
            config_manager.update_secret("plugins.echo.api_key", "stale-writer")

        assert config_manager.snapshot().secrets_status is ConfigSourceStatus.VALID
        assert config_manager.snapshot().mutable_secrets() == original.mutable_secrets()
        assert config_manager.get_plugin_secret("echo", "api_key") == "test_key"
        assert config_manager._pending_secrets_source is not None
        assert json.loads(secrets_file.read_text(encoding="utf-8")) == revoked

        confirmed = config_manager.reload()
        assert confirmed.secrets_status is ConfigSourceStatus.VALID
        assert confirmed.secrets["admin_user_ids"] == ()
        with pytest.raises(KeyError, match="键不存在"):
            config_manager.update_secret("plugins.echo.api_key", "stale-writer")
        assert json.loads(secrets_file.read_text(encoding="utf-8")) == revoked

    def test_recreated_primary_requires_refresh_then_allows_retry(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        secrets_file.unlink()
        with pytest.raises(ConfigLoadError):
            config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "first")

        recreated = {"admin_user_ids": [], "plugins": {"external": {"version": 2}}}
        secrets_file.write_text(json.dumps(recreated), encoding="utf-8")
        with pytest.raises(ConfigLoadError, match="changed on disk"):
            config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "second")

        assert config_manager.snapshot().secrets_status is ConfigSourceStatus.INCONSISTENT
        assert config_manager.secrets == {}
        config_manager.reload()
        config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "third")
        disk = json.loads(secrets_file.read_text(encoding="utf-8"))
        assert disk["plugins"]["external"]["version"] == 2
        assert disk["plugins"]["qingssh"]["passwords"]["ref-1"] == "third"

    @pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_mutation_never_changes_live_or_disk(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        non_finite: float,
    ):
        before = config_manager.snapshot()
        before_disk = secrets_file.read_bytes()

        with pytest.raises(ValueError):
            config_manager.update_secret("admin_user_ids", [non_finite])

        after = config_manager.snapshot()
        assert after.mutable_secrets() == before.mutable_secrets()
        assert after.revision == before.revision
        assert secrets_file.read_bytes() == before_disk

    @pytest.mark.parametrize("limit_kind", ["depth", "nodes", "bytes"])
    def test_mutation_limit_failure_never_changes_live_or_disk(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        limit_kind: str,
    ):
        if limit_kind == "depth":
            candidate: Any = "leaf"
            for _ in range(_MAX_CONFIG_TREE_DEPTH + 1):
                candidate = {"child": candidate}
        elif limit_kind == "nodes":
            candidate = [0] * _MAX_CONFIG_TREE_NODES
        else:
            candidate = "x" * _MAX_CONFIG_SOURCE_BYTES

        before = config_manager.snapshot()
        before_disk = secrets_file.read_bytes()
        before_sources = (
            config_manager._config_source.signature,
            config_manager._secrets_source.signature,
        )
        before_pair = (
            config_manager._paired_config_signature,
            config_manager._paired_secrets_signature,
        )

        with pytest.raises(ValueError):
            config_manager.set_plugin_secret("qingssh", "limits.candidate", candidate)

        after = config_manager.snapshot()
        assert after.revision == before.revision
        assert after.mutable_config() == before.mutable_config()
        assert after.mutable_secrets() == before.mutable_secrets()
        assert secrets_file.read_bytes() == before_disk
        assert (
            config_manager._config_source.signature,
            config_manager._secrets_source.signature,
        ) == before_sources
        assert (
            config_manager._paired_config_signature,
            config_manager._paired_secrets_signature,
        ) == before_pair

    @pytest.mark.parametrize("interference", ["replace", "delete", "same-inode-overwrite"])
    def test_write_interference_is_detected_and_external_state_wins(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        interference: str,
    ):
        from core import config as config_module

        real_write = config_module._write_secret_payload
        external = {
            "admin_user_ids": [7070],
            "plugins": {"external": {"interference": interference}},
        }
        external_payload = json.dumps(external).encode("utf-8")
        before = config_manager.snapshot()

        def interfere(handle: Any, payload: bytes) -> None:
            if interference == "replace":
                replacement = secrets_file.with_suffix(".replacement")
                replacement.write_bytes(external_payload)
                # Closing the pinned handle makes this deterministic on Windows;
                # the production post-write identity check must still reject it.
                handle.close()
                os.replace(replacement, secrets_file)
            elif interference == "delete":
                handle.close()
                secrets_file.unlink()
            else:
                real_write(handle, payload)
                real_write(handle, external_payload)

        monkeypatch.setattr(config_module, "_write_secret_payload", interfere)

        with pytest.raises(ConfigLoadError, match="changed on disk"):
            config_manager.update_secret("admin_user_ids", [999])

        snapshot = config_manager.snapshot()
        if interference == "delete":
            assert snapshot.secrets == {}
            assert snapshot.secrets_status is ConfigSourceStatus.MISSING
            assert not secrets_file.exists()
        else:
            assert snapshot.secrets_status is ConfigSourceStatus.VALID
            assert snapshot.mutable_secrets() == before.mutable_secrets()
            assert config_manager._pending_secrets_source is not None
            assert config_manager._pending_secrets_source.value == external
            assert json.loads(secrets_file.read_text(encoding="utf-8")) == external
        assert snapshot.mutable_secrets() != {"admin_user_ids": [999]}

    def test_tuple_input_is_canonicalized_and_live_matches_disk(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        config_manager.update_secret("admin_user_ids", (11, 22))
        config_manager.set_plugin_secret(
            "qingssh",
            "passwords.ordered",
            ("first", {"nested": [1, 2]}),
        )

        disk = json.loads(secrets_file.read_text(encoding="utf-8"))
        snapshot = config_manager.snapshot()
        assert snapshot.secrets["admin_user_ids"] == (11, 22)
        ordered = snapshot.secrets["plugins"]["qingssh"]["passwords"]["ordered"]
        assert isinstance(ordered, tuple)
        assert ordered[0] == "first"
        assert ordered[1]["nested"] == (1, 2)
        assert snapshot.mutable_secrets() == disk
        assert disk["plugins"]["qingssh"]["passwords"]["ordered"] == [
            "first",
            {"nested": [1, 2]},
        ]
