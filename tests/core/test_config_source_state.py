"""配置来源状态模型。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    _MAX_CONFIG_SOURCE_BYTES,
    _MAX_CONFIG_TREE_DEPTH,
    _MAX_CONFIG_TREE_NODES,
    Any,
    ConfigLoadError,
    ConfigManager,
    ConfigSourceStatus,
    Path,
    json,
    pytest,
)

config_file     = _fixture_support.config_file
config_manager  = _fixture_support.config_manager
secrets_file    = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigSourceStateModel:
    """Regression matrix for independent config and secrets source policies."""

    def test_snapshot_reports_valid_initial_sources(self, config_manager: ConfigManager):
        snapshot = config_manager.snapshot()

        assert snapshot.config_status is ConfigSourceStatus.VALID
        assert snapshot.secrets_status is ConfigSourceStatus.VALID
        assert snapshot.revision == 0

    def test_config_deletion_keeps_lkg_while_secrets_deletion_fails_closed(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
    ):
        original_config  = config_manager.snapshot().mutable_config()
        original_secrets = config_manager.snapshot().mutable_secrets()

        config_file.unlink()
        config_missing = config_manager.reload()

        assert config_missing.config_status is ConfigSourceStatus.MISSING
        assert config_missing.secrets_status is ConfigSourceStatus.VALID
        assert config_missing.mutable_config() == original_config
        assert config_missing.mutable_secrets() == original_secrets

        secrets_file.unlink()
        both_missing = config_manager.reload()

        assert both_missing.config_status is ConfigSourceStatus.MISSING
        assert both_missing.secrets_status is ConfigSourceStatus.MISSING
        assert both_missing.mutable_config() == original_config
        assert both_missing.mutable_secrets() == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"{not-json", id="json"),
            pytest.param(b"\xff\xfe", id="utf8"),
            pytest.param(
                json.dumps({"bot_name": "candidate", "max_concurrency": 0}).encode(),
                id="schema",
            ),
        ],
    )
    def test_invalid_config_keeps_lkg_and_records_invalid_status(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        payload: bytes,
    ):
        before = config_manager.snapshot()
        config_file.write_bytes(payload)

        with pytest.raises(ConfigLoadError):
            config_manager.reload()

        after = config_manager.snapshot()
        assert after.config_status is ConfigSourceStatus.INVALID
        assert after.secrets_status is ConfigSourceStatus.VALID
        assert after.mutable_config() == before.mutable_config()
        assert after.mutable_secrets() == before.mutable_secrets()
        assert after.revision == before.revision + 1

    def test_invalid_config_cannot_pair_lkg_with_rotated_secrets(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
    ):
        before  = config_manager.snapshot()
        rotated = {
            "admin_user_ids": [999],
            "plugins": {"echo": {"api_key": "for-new-config-only"}},
        }
        config_file.write_text("{broken", encoding="utf-8")
        secrets_file.write_text(json.dumps(rotated), encoding="utf-8")

        with pytest.raises(ConfigLoadError):
            config_manager.reload()

        rejected = config_manager.snapshot()
        assert rejected.config_status is ConfigSourceStatus.INVALID
        assert rejected.secrets_status is ConfigSourceStatus.INCONSISTENT
        assert rejected.mutable_config() == before.mutable_config()
        assert rejected.mutable_secrets() == {}

        repaired_config             = before.mutable_config()
        repaired_config["bot_name"] = "paired-new-config"
        config_file.write_text(json.dumps(repaired_config), encoding="utf-8")
        confirmed = config_manager.reload()

        assert confirmed.config_status is ConfigSourceStatus.VALID
        assert confirmed.secrets_status is ConfigSourceStatus.VALID
        assert confirmed.config["bot_name"] == "paired-new-config"
        assert confirmed.mutable_secrets() == rotated

    def test_initial_invalid_config_never_authorizes_valid_secrets(self, tmp_path: Path):
        config_path  = tmp_path / "invalid-initial-config.json"
        secrets_path = tmp_path / "valid-initial-secrets.json"
        config_path.write_text("{broken", encoding="utf-8")
        secrets_path.write_text(json.dumps({"onebot_token": "must-not-authorize"}))

        manager  = ConfigManager(config_path, secrets_path)
        snapshot = manager.snapshot()

        assert snapshot.config_status is ConfigSourceStatus.INVALID
        assert snapshot.secrets_status is ConfigSourceStatus.INCONSISTENT
        assert snapshot.secrets == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"{not-json", id="json"),
            pytest.param(b"\xff\xfe", id="utf8"),
            pytest.param(b'{"token":NaN}', id="nan"),
            pytest.param(b'{"token":Infinity}', id="infinity-token"),
            pytest.param(b'{"token":1e9999}', id="overflow-infinity"),
        ],
    )
    def test_invalid_secrets_fail_closed_without_blocking_valid_config(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        payload: bytes,
    ):
        before = config_manager.snapshot()
        secrets_file.write_bytes(payload)

        after = config_manager.reload()

        assert after.config_status is ConfigSourceStatus.VALID
        assert after.secrets_status is ConfigSourceStatus.INVALID
        assert after.mutable_config() == before.mutable_config()
        assert after.mutable_secrets() == {}
        assert config_manager.get_plugin_secret("echo", "api_key") is None
        assert after.revision == before.revision + 1

    def test_config_fault_and_secret_deletion_publish_one_fail_closed_revision(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
    ):
        before = config_manager.snapshot()
        config_file.write_text("{broken", encoding="utf-8")
        secrets_file.unlink()

        with pytest.raises(ConfigLoadError):
            config_manager.reload()

        after = config_manager.snapshot()
        assert after.config_status is ConfigSourceStatus.INVALID
        assert after.secrets_status is ConfigSourceStatus.MISSING
        assert after.mutable_config() == before.mutable_config()
        assert after.mutable_secrets() == {}
        assert after.revision == before.revision + 1

    def test_unavailable_secrets_fail_closed_and_recover(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        real_open = Path.open

        def permission_denied(path: Path, *args: Any, **kwargs: Any):
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == secrets_file and mode == "rb":
                raise PermissionError(13, "denied", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", permission_denied)
        unavailable = config_manager.reload()

        assert unavailable.secrets_status is ConfigSourceStatus.UNAVAILABLE
        assert unavailable.mutable_secrets() == {}

        monkeypatch.setattr(Path, "open", real_open)
        recovered = config_manager.reload()
        assert recovered.secrets_status is ConfigSourceStatus.VALID
        assert recovered.secrets["plugins"]["echo"]["api_key"] == "test_key"

    def test_primary_parse_failure_never_restores_backup(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        secrets_file: Path,
    ):
        config_backup  = config_file.with_name(f"{config_file.name}.bak")
        secrets_backup = secrets_file.with_name(f"{secrets_file.name}.bak")
        config_backup.write_text('{"bot_name":"backup-config"}', encoding="utf-8")
        secrets_backup.write_text('{"token":"revoked-backup"}', encoding="utf-8")
        invalid_config  = b"{invalid-config"
        invalid_secrets = b"{invalid-secrets"
        config_file.write_bytes(invalid_config)
        secrets_file.write_bytes(invalid_secrets)

        with pytest.raises(ConfigLoadError):
            config_manager.reload()

        snapshot = config_manager.snapshot()
        assert snapshot.config_status is ConfigSourceStatus.INVALID
        assert snapshot.secrets_status is ConfigSourceStatus.INVALID
        assert snapshot.secrets == {}
        assert config_file.read_bytes() == invalid_config
        assert secrets_file.read_bytes() == invalid_secrets
        assert json.loads(config_backup.read_text(encoding="utf-8"))["bot_name"] == "backup-config"
        assert json.loads(secrets_backup.read_text(encoding="utf-8"))["token"] == "revoked-backup"

    def test_oversize_source_is_invalid_and_secrets_fail_closed(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        secrets_file.write_bytes(b" " * (_MAX_CONFIG_SOURCE_BYTES + 1))

        snapshot = config_manager.reload()

        assert snapshot.secrets_status is ConfigSourceStatus.INVALID
        assert snapshot.secrets == {}

    def test_tree_over_depth_limit_is_invalid_and_secrets_fail_closed(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        nested: Any = "leaf"
        for _ in range(_MAX_CONFIG_TREE_DEPTH + 1):
            nested = {"child": nested}
        secrets_file.write_text(json.dumps({"nested": nested}), encoding="utf-8")

        snapshot = config_manager.reload()

        assert snapshot.secrets_status is ConfigSourceStatus.INVALID
        assert snapshot.secrets == {}

    def test_tree_over_node_limit_is_invalid_and_secrets_fail_closed(
        self,
        config_manager: ConfigManager,
        secrets_file: Path,
    ):
        payload = {"values": [0] * _MAX_CONFIG_TREE_NODES}
        secrets_file.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = config_manager.reload()

        assert snapshot.secrets_status is ConfigSourceStatus.INVALID
        assert snapshot.secrets == {}
