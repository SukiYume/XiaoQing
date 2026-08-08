"""快照、热重载和权限。"""

from __future__ import annotations

import tests.helpers.config_test_support as _fixture_support
from tests.helpers.config_test_support import (
    Any,
    ConfigManager,
    ConfigSnapshot,
    ConfigSourceStatus,
    Path,
    _check_secrets_file_permissions,
    json,
    logging,
    materialize_snapshot_value,
    platform,
    pytest,
)

config_file = _fixture_support.config_file
config_manager = _fixture_support.config_manager
secrets_file = _fixture_support.secrets_file
temp_config_dir = _fixture_support.temp_config_dir


class TestConfigSnapshot:
    """ConfigSnapshot 数据类测试"""

    def test_create_snapshot(self):
        """测试创建快照"""
        snapshot = ConfigSnapshot(config={"key": "value"}, secrets={"secret": "hidden"})
        assert snapshot.config == {"key": "value"}
        assert snapshot.secrets == {"secret": "hidden"}

    def test_builtin_base_methods_cannot_mutate_snapshot_containers(self):
        snapshot = ConfigSnapshot(
            config={"nested": {"enabled": True}, "items": [1, 2]},
            secrets={"plugins": {"demo": {"token": "secret"}}},
            revision=7,
        )

        with pytest.raises(TypeError):
            dict.__setitem__(snapshot.config, "new", "value")
        with pytest.raises(TypeError):
            dict.update(snapshot.config["nested"], {"enabled": False})
        with pytest.raises(TypeError):
            dict.__ior__(snapshot.config, {"new": "value"})
        with pytest.raises(TypeError):
            list.__setitem__(snapshot.config["items"], 0, 99)
        with pytest.raises(TypeError):
            list.append(snapshot.config["items"], 3)
        with pytest.raises(TypeError):
            list.__iadd__(snapshot.config["items"], [3])
        with pytest.raises((AttributeError, TypeError)):
            object.__setattr__(snapshot, "revision", 999)

        rebound_items = snapshot.config["items"]
        rebound_items += (3,)
        repeated_items = snapshot.config["items"]
        repeated_items *= 2
        with pytest.raises(TypeError):
            rebound_mapping = snapshot.config
            rebound_mapping |= {"new": "detached"}
        merged = snapshot.config | {"new": "detached"}
        assert rebound_items == (1, 2, 3)
        assert repeated_items == (1, 2, 1, 2)
        assert merged["new"] == "detached"
        assert snapshot.config["items"] == (1, 2)
        assert "new" not in snapshot.config
        assert snapshot.config["nested"]["enabled"] is True
        assert snapshot.revision == 7

    def test_constructor_detaches_mutable_inputs(self):
        config = {"nested": {"items": [1]}}
        secrets = {"plugins": {"demo": {"token": "before"}}}
        snapshot = ConfigSnapshot(config=config, secrets=secrets)

        config["nested"]["items"].append(2)
        secrets["plugins"]["demo"]["token"] = "after"

        assert snapshot.config["nested"]["items"] == (1,)
        assert snapshot.secrets["plugins"]["demo"]["token"] == "before"

    def test_materialize_snapshot_value_returns_detached_mutable_containers(self):
        snapshot = ConfigSnapshot(
            config={"nested": {"items": [1, {"enabled": True}]}},
            secrets={},
        )

        materialized = materialize_snapshot_value(snapshot.config["nested"])
        materialized["items"][1]["enabled"] = False
        materialized["items"].append(2)

        assert materialized == {"items": [1, {"enabled": False}, 2]}
        assert snapshot.config["nested"] == {"items": (1, {"enabled": True})}

    def test_replace_snapshot_detaches_caller_aliases(self, config_manager: ConfigManager):
        config = {"nested": {"items": [1]}}
        secrets = {"plugins": {"demo": {"token": "before"}}}
        config_manager._replace_snapshot(config, secrets)

        config["nested"]["items"].append(2)
        secrets["plugins"]["demo"]["token"] = "after"

        assert config_manager.config["nested"]["items"] == (1,)
        assert config_manager.secrets["plugins"]["demo"]["token"] == "before"

    @pytest.mark.parametrize(
        ("candidate_config", "candidate_secrets"),
        [
            ({"bad": float("inf")}, {"token": "candidate"}),
            ({"bot_name": "candidate"}, {"bad": float("inf")}),
        ],
    )
    def test_replace_snapshot_failure_is_atomic(
        self,
        config_manager: ConfigManager,
        candidate_config: dict[str, Any],
        candidate_secrets: dict[str, Any],
    ):
        before_config = config_manager.config
        before_secrets = config_manager.secrets
        before_internal_config = config_manager.snapshot().mutable_config()
        before_internal_secrets = config_manager.snapshot().mutable_secrets()
        before_revision = config_manager.revision
        before_generation = config_manager._source_generation
        before_sources = (
            config_manager._config_source.signature,
            config_manager._secrets_source.signature,
        )

        with pytest.raises(ValueError, match="must be finite"):
            config_manager._replace_snapshot(candidate_config, candidate_secrets)

        assert config_manager.config is before_config
        assert config_manager.secrets is before_secrets
        assert config_manager.snapshot().mutable_config() == before_internal_config
        assert config_manager.snapshot().mutable_secrets() == before_internal_secrets
        assert config_manager.revision == before_revision
        assert config_manager._source_generation == before_generation
        assert (
            config_manager._config_source.signature,
            config_manager._secrets_source.signature,
        ) == before_sources


class TestCheckSecretsFilePermissions:
    """_check_secrets_file_permissions 测试"""

    def test_skips_nonexistent_file(self, tmp_path: Path, caplog):
        """测试跳过不存在的文件"""
        nonexistent = tmp_path / "nonexistent.json"
        _check_secrets_file_permissions(nonexistent)
        # 不应该抛出异常

    def test_logs_on_windows(self, tmp_path: Path, caplog, monkeypatch):
        """测试 Windows 上的日志"""
        # 模拟 Windows
        monkeypatch.setattr(platform, "system", lambda: "Windows")

        test_file = tmp_path / "secrets.json"
        test_file.write_text("{}")

        with caplog.at_level(logging.INFO):
            _check_secrets_file_permissions(test_file)

        # 应该记录 info 级别日志（消息中包含 "Running on Windows"）
        assert any("Running on Windows" in record.message for record in caplog.records)


class TestConfigHotReload:
    """测试配置热加载功能"""

    @pytest.mark.asyncio
    async def test_config_reload(
        self, temp_config_dir: Path, config_file: Path, secrets_file: Path
    ):
        """测试配置重新加载"""
        manager = ConfigManager(config_file, secrets_file)
        initial_value = manager.config.get("bot_name")

        # 修改配置文件
        new_config = json.loads(config_file.read_text(encoding="utf-8"))
        new_config["bot_name"] = "新名字"
        config_file.write_text(
            json.dumps(new_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 重新加载
        manager.reload()
        assert manager.config.get("bot_name") == "新名字"
        assert manager.config.get("bot_name") != initial_value

    @pytest.mark.asyncio
    async def test_invalid_config_handling(self, temp_config_dir: Path):
        """测试无效配置处理"""
        # 创建无效的 config 文件
        invalid_config = temp_config_dir / "invalid_config.json"
        invalid_config.write_text("{invalid json}", encoding="utf-8")

        invalid_secrets = temp_config_dir / "invalid_secrets.json"

        # Initial load records the invalid primary and starts from an empty LKG.
        manager = ConfigManager(invalid_config, invalid_secrets)
        assert manager.config == {}
        assert manager.secrets == {}
        assert manager.snapshot().config_status is ConfigSourceStatus.INVALID
        assert manager.snapshot().secrets_status is ConfigSourceStatus.MISSING

    @pytest.mark.asyncio
    async def test_missing_secrets_fallback(self, temp_config_dir: Path):
        """测试缺失secrets.json的降级处理"""
        config_file = temp_config_dir / "config.json"
        config_data = {"bot_name": "测试机器人"}
        config_file.write_text(
            json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 使用不存在的 secrets 文件
        missing_secrets = temp_config_dir / "nonexistent_secrets.json"

        # ConfigManager 应该能处理缺失的 secrets 文件
        manager = ConfigManager(config_file, missing_secrets)
        assert manager.secrets == {}
        assert manager.config.get("bot_name") == "测试机器人"
