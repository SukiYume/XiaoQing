"""配置初始化和重载。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    Any,
    ConfigLoadError,
    ConfigManager,
    ConfigSourceStatus,
    Mapping,
    Path,
    _validate_runtime_config,
    json,
    pytest,
)

config_file = _fixture_support.config_file
config_manager = _fixture_support.config_manager
secrets_file = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigManagerInit:
    """ConfigManager 初始化测试"""

    def test_initialization(self, config_file: Path, secrets_file: Path):
        """测试初始化"""
        manager = ConfigManager(config_file, secrets_file)
        assert manager.config_path == config_file
        assert manager.secrets_path == secrets_file
        assert isinstance(manager.config, Mapping)
        assert isinstance(manager.secrets, Mapping)

    def test_loads_config_on_init(self, config_manager: ConfigManager):
        """测试初始化时加载配置"""
        config = config_manager.config
        assert config["bot_name"] == "测试机器人"
        assert config["command_prefixes"] == ("/", "!")
        assert config["require_bot_name_in_group"] is True

    def test_loads_secrets_on_init(self, config_manager: ConfigManager):
        """测试初始化时加载密钥"""
        secrets = config_manager.secrets
        assert secrets["admin_user_ids"] == (12345, 67890)
        assert secrets["plugins"]["echo"]["api_key"] == "test_key"

    def test_returns_read_only_config_snapshot(self, config_manager: ConfigManager):
        """测试返回只读配置快照"""
        config1 = config_manager.config
        config2 = config_manager.config
        assert config1 is config2
        assert config1 == config2
        with pytest.raises(TypeError):
            config1["bot_name"] = "changed"

    def test_returns_read_only_secrets_snapshot(self, config_manager: ConfigManager):
        """测试返回只读密钥快照"""
        secrets1 = config_manager.secrets
        secrets2 = config_manager.secrets
        assert secrets1 is secrets2
        assert secrets1 == secrets2
        with pytest.raises(TypeError):
            secrets1["admin_user_ids"] = []

    def test_config_nested_mutation_does_not_affect_internal_state(
        self, config_manager: ConfigManager
    ):
        config = config_manager.config
        with pytest.raises(TypeError):
            config["plugins"]["echo"]["enabled"] = False

        current = config_manager.config
        assert current["plugins"]["echo"]["enabled"] is True

    def test_snapshot_mutable_exports_are_detached(self, config_manager: ConfigManager):
        snapshot = config_manager.snapshot()
        config = snapshot.mutable_config()
        secrets = snapshot.mutable_secrets()
        config["plugins"]["echo"]["enabled"] = False
        secrets["plugins"]["echo"]["api_key"] = "changed"

        assert config_manager.config["plugins"]["echo"]["enabled"] is True
        assert config_manager.secrets["plugins"]["echo"]["api_key"] == "test_key"


class TestConfigManagerReload:
    """ConfigManager.reload 测试"""

    def test_reload_updates_config(self, config_manager: ConfigManager, config_file: Path):
        """测试重新加载配置"""
        # 修改文件
        new_data = {"bot_name": "新名称", "command_prefixes": ["#"]}
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2)

        # 重新加载
        config_manager.reload()

        # 验证
        assert config_manager.config["bot_name"] == "新名称"
        assert config_manager.config["command_prefixes"] == ("#",)

    def test_reload_updates_secrets(self, config_manager: ConfigManager, secrets_file: Path):
        """测试重新加载密钥"""
        # 修改文件
        new_data = {"admin_user_ids": [99999]}
        with open(secrets_file, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2)

        config_manager.reload()

        assert config_manager.secrets["admin_user_ids"] == (99999,)

    def test_reload_handles_missing_files(self, temp_config_dir: Path):
        """测试处理缺失文件"""
        missing_config = temp_config_dir / "nonexistent_config.json"
        missing_secrets = temp_config_dir / "nonexistent_secrets.json"

        manager = ConfigManager(missing_config, missing_secrets)
        assert manager.config == {}
        assert manager.secrets == {}

    def test_reload_keeps_last_valid_snapshot_when_config_is_invalid(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        """测试 reload 遇到损坏配置时保留旧快照"""
        original = config_manager.config
        config_file.write_text("{invalid json}", encoding="utf-8")

        with pytest.raises(ConfigLoadError):
            config_manager.reload()

        assert config_manager.config == original

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_concurrency", 0),
            ("plugin_poll_interval", -1),
            ("ws_queue_size", 0),
            ("inbound_ws_broadcast_timeout_seconds", 0),
            ("inbound_ws_broadcast_timeout_seconds", 301),
            ("timezone", "Mars/Olympus"),
            ("onebot_http_base", "ftp://example.com"),
            ("inbound_http_base", "https://127.0.0.1:12000"),
            ("inbound_ws_uri", "wss://127.0.0.1:12000/ws"),
            ("inbound_http_base", "http://0.0.0.0:12000"),
            ("inbound_trusted_tls_proxy", "true"),
        ],
    )
    def test_reload_rejects_invalid_runtime_field_before_replacing_snapshot(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        field: str,
        value: Any,
    ):
        original = config_manager.config
        candidate = config_manager.snapshot().mutable_config()
        candidate[field] = value
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="Invalid runtime configuration"):
            config_manager.reload()

        assert config_manager.config == original

    @pytest.mark.parametrize(
        ("http_base", "ws_uri"),
        [
            ("http://localhost:12000", "ws://localhost:12000/ws"),
            ("http://127.0.0.1:12000", "ws://127.0.0.1:12000/ws"),
            ("http://[::1]:12000", "ws://[::1]:12000/ws"),
        ],
    )
    def test_reload_accepts_plaintext_inbound_on_loopback(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        http_base: str,
        ws_uri: str,
    ):
        candidate = config_manager.snapshot().mutable_config()
        candidate.update(
            {
                "inbound_http_base": http_base,
                "inbound_ws_uri": ws_uri,
            }
        )
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        config_manager.reload()

        assert config_manager.config["inbound_http_base"] == http_base
        assert config_manager.config["inbound_ws_uri"] == ws_uri

    def test_reload_accepts_nonempty_plugin_data_root(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        candidate = config_manager.snapshot().mutable_config()
        candidate["data_root"] = "runtime/plugin-data"
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        config_manager.reload()

        assert config_manager.config["data_root"] == "runtime/plugin-data"

    @pytest.mark.parametrize("value", ["", "   ", "bad\x00path", 123])
    def test_reload_rejects_invalid_plugin_data_root(
        self,
        config_manager: ConfigManager,
        config_file: Path,
        value: object,
    ):
        candidate = config_manager.snapshot().mutable_config()
        candidate["data_root"] = value
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="data_root"):
            config_manager.reload()

    def test_reload_accepts_non_loopback_only_with_trusted_tls_proxy_acknowledgement(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        candidate = config_manager.snapshot().mutable_config()
        candidate.update(
            {
                "inbound_http_base": "http://0.0.0.0:12000",
                "inbound_ws_uri": "ws://0.0.0.0:12000/ws",
                "inbound_trusted_tls_proxy": True,
            }
        )
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        config_manager.reload()

        assert config_manager.config["inbound_trusted_tls_proxy"] is True

    def test_reload_rejects_enabled_ws_without_uri_before_replacing_snapshot(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        original = config_manager.config
        candidate = config_manager.snapshot().mutable_config()
        candidate["enable_ws_client"] = True
        candidate["onebot_ws_uri"] = ""
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="onebot_ws_uri"):
            config_manager.reload()

        assert config_manager.config == original

    def test_reload_rejects_invalid_plugin_execution_before_replacing_snapshot(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        original = config_manager.snapshot()
        candidate = config_manager.snapshot().mutable_config()
        candidate["plugin_execution"] = {
            "sync_parallel_limit": 4,
            "overrides": {"demo": {"global_sync_queue_limit": 8}},
        }
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="plugin_execution"):
            config_manager.reload()

        rejected = config_manager.snapshot()
        assert rejected.mutable_config() == original.mutable_config()
        assert rejected.revision == original.revision + 1
        assert rejected.config_status is ConfigSourceStatus.INVALID
        assert rejected.secrets_status is ConfigSourceStatus.VALID

    def test_example_config_matches_runtime_schema(self):
        example_path = Path(__file__).resolve().parents[1] / "config" / "config.json.example"
        example = json.loads(example_path.read_text(encoding="utf-8"))

        validated = _validate_runtime_config(example)

        assert validated["inbound_trusted_tls_proxy"] is False
        assert validated["inbound_ws_broadcast_timeout_seconds"] == 5.0
        assert validated["plugin_execution"]["sync_parallel_limit"] <= 3
        assert validated["plugin_execution"]["global_sync_queue_limit"] >= 1

    def test_plugin_execution_accepts_strict_global_and_override_limits(self):
        policy = {
            "timeout_seconds": 0,
            "parallel_limit": 1024,
            "admission_queue_limit": 0,
            "sync_parallel_limit": 3,
            "sync_queue_limit": 10000,
            "failure_threshold": 10000,
            "cooldown_seconds": 86400,
            "drain_timeout_seconds": 3600,
            "global_sync_queue_limit": 100000,
            "overrides": {
                "demo_2": {
                    "timeout_seconds": 0.1,
                    "sync_parallel_limit": 1,
                    "sync_queue_limit": 0,
                }
            },
        }

        validated = _validate_runtime_config({"plugin_execution": policy})

        assert validated["plugin_execution"] == policy

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("timeout_seconds", 0.01),
            ("timeout_seconds", 86400.1),
            ("timeout_seconds", None),
            ("timeout_seconds", "60"),
            ("parallel_limit", 0),
            ("parallel_limit", 1025),
            ("parallel_limit", True),
            ("parallel_limit", 4.0),
            ("parallel_limit", "4"),
            ("admission_queue_limit", -1),
            ("admission_queue_limit", 10001),
            ("admission_queue_limit", False),
            ("sync_parallel_limit", 0),
            ("sync_parallel_limit", 4),
            ("sync_queue_limit", -1),
            ("sync_queue_limit", 10001),
            ("sync_queue_limit", "16"),
            ("failure_threshold", 0),
            ("failure_threshold", 10001),
            ("cooldown_seconds", 0),
            ("cooldown_seconds", 86400.1),
            ("cooldown_seconds", float("inf")),
            ("drain_timeout_seconds", 0),
            ("drain_timeout_seconds", 3600.1),
            ("drain_timeout_seconds", float("nan")),
            ("global_sync_queue_limit", 0),
            ("global_sync_queue_limit", 100001),
            ("global_sync_queue_limit", "256"),
        ],
    )
    def test_plugin_execution_rejects_invalid_or_coerced_limits(
        self,
        field: str,
        value: Any,
    ):
        with pytest.raises(ConfigLoadError, match="plugin_execution"):
            _validate_runtime_config({"plugin_execution": {field: value}})

    @pytest.mark.parametrize(
        "plugin_execution",
        [
            None,
            [],
            {"unknown_limit": 1},
            {"overrides": None},
            {"overrides": {"bad-name": {"parallel_limit": 1}}},
            {"overrides": {"": {"parallel_limit": 1}}},
            {"overrides": {"插件": {"parallel_limit": 1}}},
            {"overrides": {"demo": {"unknown_limit": 1}}},
            {"overrides": {"demo": {"global_sync_queue_limit": 8}}},
            {"overrides": {"demo": {"overrides": {}}}},
        ],
    )
    def test_plugin_execution_rejects_unknown_shape_or_invalid_override(
        self,
        plugin_execution: Any,
    ):
        with pytest.raises(ConfigLoadError, match="plugin_execution"):
            _validate_runtime_config({"plugin_execution": plugin_execution})

    def test_plugin_execution_override_omits_unspecified_fields(self):
        validated = _validate_runtime_config(
            {
                "plugin_execution": {
                    "parallel_limit": 8,
                    "overrides": {"demo": {"timeout_seconds": 0}},
                }
            }
        )

        assert validated["plugin_execution"]["overrides"]["demo"] == {"timeout_seconds": 0.0}
