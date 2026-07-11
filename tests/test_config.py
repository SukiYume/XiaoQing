"""
ConfigManager 单元测试
"""

import json
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any

import pytest

from core.config import (
    ConfigLoadError,
    ConfigManager,
    ConfigSnapshot,
    _check_secrets_file_permissions,
    _validate_runtime_config,
)

# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """创建临时配置目录"""
    return tmp_path


@pytest.fixture
def config_file(temp_config_dir: Path) -> Path:
    """创建配置文件"""
    config_path = temp_config_dir / "config.json"
    config_data = {
        "bot_name": "测试机器人",
        "command_prefixes": ["/", "!"],
        "require_bot_name_in_group": True,
        "random_reply_rate": 0.1,
        "plugins": {
            "echo": {"enabled": True},
            "choice": {"enabled": False},
        },
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2, ensure_ascii=False)
    return config_path


@pytest.fixture
def secrets_file(temp_config_dir: Path) -> Path:
    """创建密钥文件"""
    secrets_path = temp_config_dir / "secrets.json"
    secrets_data = {
        "admin_user_ids": [12345, 67890],
        "plugins": {
            "echo": {"api_key": "test_key"},
            "choice": {},
        },
    }
    with open(secrets_path, "w", encoding="utf-8") as f:
        json.dump(secrets_data, f, indent=2, ensure_ascii=False)
    return secrets_path


@pytest.fixture
def config_manager(config_file: Path, secrets_file: Path) -> ConfigManager:
    """创建 ConfigManager 实例"""
    return ConfigManager(config_file, secrets_file)

# ============================================================
# ConfigManager 初始化测试
# ============================================================

class TestConfigManagerInit:
    """ConfigManager 初始化测试"""

    def test_initialization(self, config_file: Path, secrets_file: Path):
        """测试初始化"""
        manager = ConfigManager(config_file, secrets_file)
        assert manager.config_path == config_file
        assert manager.secrets_path == secrets_file
        assert isinstance(manager.config, dict)
        assert isinstance(manager.secrets, dict)

    def test_loads_config_on_init(self, config_manager: ConfigManager):
        """测试初始化时加载配置"""
        config = config_manager.config
        assert config["bot_name"] == "测试机器人"
        assert config["command_prefixes"] == ["/", "!"]
        assert config["require_bot_name_in_group"] is True

    def test_loads_secrets_on_init(self, config_manager: ConfigManager):
        """测试初始化时加载密钥"""
        secrets = config_manager.secrets
        assert secrets["admin_user_ids"] == [12345, 67890]
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

    def test_config_nested_mutation_does_not_affect_internal_state(self, config_manager: ConfigManager):
        config = config_manager.config
        with pytest.raises(TypeError):
            config["plugins"]["echo"]["enabled"] = False

        current = config_manager.config
        assert current["plugins"]["echo"]["enabled"] is True

    def test_snapshot_returns_deep_copy(self, config_manager: ConfigManager):
        snapshot = config_manager.snapshot()
        snapshot.config["plugins"]["echo"]["enabled"] = False
        snapshot.secrets["plugins"]["echo"]["api_key"] = "changed"

        assert config_manager.config["plugins"]["echo"]["enabled"] is True
        assert config_manager.secrets["plugins"]["echo"]["api_key"] == "test_key"

# ============================================================
# ConfigManager.reload 测试
# ============================================================

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
        assert config_manager.config["command_prefixes"] == ["#"]

    def test_reload_updates_secrets(self, config_manager: ConfigManager, secrets_file: Path):
        """测试重新加载密钥"""
        # 修改文件
        new_data = {"admin_user_ids": [99999]}
        with open(secrets_file, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2)

        config_manager.reload()

        assert config_manager.secrets["admin_user_ids"] == [99999]

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
        candidate = dict(original)
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
        candidate = dict(config_manager.config)
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

    def test_reload_accepts_non_loopback_only_with_trusted_tls_proxy_acknowledgement(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        candidate = dict(config_manager.config)
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
        candidate = dict(original)
        candidate["enable_ws_client"] = True
        candidate["onebot_ws_uri"] = ""
        config_file.write_text(json.dumps(candidate), encoding="utf-8")

        with pytest.raises(ConfigLoadError, match="onebot_ws_uri"):
            config_manager.reload()

        assert config_manager.config == original

    def test_example_config_matches_runtime_schema(self):
        example_path = Path(__file__).resolve().parents[1] / "config" / "config.json.example"
        example = json.loads(example_path.read_text(encoding="utf-8"))

        validated = _validate_runtime_config(example)

        assert validated["inbound_trusted_tls_proxy"] is False

# ============================================================
# ConfigManager.update_secret 测试
# ============================================================

class TestConfigManagerUpdateSecret:
    """ConfigManager.update_secret 测试"""

    def test_update_existing_value(self, config_manager: ConfigManager, secrets_file: Path):
        """测试更新已存在的值"""
        config_manager.update_secret("admin_user_ids", [11111, 22222])

        assert config_manager.secrets["admin_user_ids"] == [11111, 22222]

    def test_update_nested_value(self, config_manager: ConfigManager):
        """测试更新嵌套值"""
        config_manager.update_secret("plugins.echo.api_key", "new_key")

        assert config_manager.secrets["plugins"]["echo"]["api_key"] == "new_key"

    def test_update_saves_to_file(self, config_manager: ConfigManager, secrets_file: Path):
        """测试更新后保存到文件"""
        config_manager.update_secret("admin_user_ids", [55555])

        # 重新读取文件验证
        with open(secrets_file, "r", encoding="utf-8") as f:
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
        assert snapshots[0].secrets["admin_user_ids"] == [2024]

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

        assert [snapshot.secrets["admin_user_ids"] for snapshot in snapshots] == [[2024]]

    def test_reload_callback_can_unsubscribe_itself_during_notification(
        self,
        config_manager: ConfigManager,
    ):
        seen: list[list[int]] = []
        unsubscribe = lambda: None

        def callback(snapshot: ConfigSnapshot) -> None:
            seen.append(list(snapshot.secrets["admin_user_ids"]))
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
        assert snapshots[0].secrets["admin_user_ids"] == [2025]

    def test_update_secret_rolls_back_when_save_fails(
        self,
        config_manager: ConfigManager,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """测试 update_secret 落盘失败会回滚内存状态"""
        original = config_manager.secrets

        def _boom() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(config_manager, "save_secrets", _boom)

        with pytest.raises(OSError, match="disk full"):
            config_manager.update_secret("admin_user_ids", [99999])

        assert config_manager.secrets == original

# ============================================================
# ConfigManager.save_secrets 测试
# ============================================================

class TestConfigManagerSaveSecrets:
    """ConfigManager.save_secrets 测试"""

    def test_save_secrets_writes_file(self, config_manager: ConfigManager, secrets_file: Path):
        """测试保存密钥到文件"""
        # 修改内部状态
        config_manager._secrets = {"test": "value"}

        config_manager.save_secrets()

        with open(secrets_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"test": "value"}

# ============================================================
# ConfigManager.on_reload 测试
# ============================================================

class TestConfigManagerOnReload:
    """ConfigManager.on_reload 测试"""

    @pytest.mark.asyncio
    async def test_reload_callback(self, config_manager: ConfigManager):
        """测试重新加载回调在手动 reload 时不触发"""
        """注意：回调只在 watch 方法中触发，不在手动 reload 中触发"""
        callbacks_called = []

        def callback(snapshot: ConfigSnapshot):
            callbacks_called.append(snapshot)

        config_manager.on_reload(callback)
        config_manager.reload()

        # 手动 reload 不触发回调（回调只通过文件监控触发）
        assert len(callbacks_called) == 0

# ============================================================
# ConfigManager.watch 测试
# ============================================================

class TestConfigManagerWatch:
    """ConfigManager.watch 测试"""

    @pytest.mark.asyncio
    async def test_watch_detects_changes(self, config_manager: ConfigManager, config_file: Path):
        """测试监控文件变化"""
        changes_detected = []

        def callback(snapshot: ConfigSnapshot):
            changes_detected.append(snapshot)

        config_manager.on_reload(callback)

        # 启动监控（短时间）
        import asyncio
        watch_task = asyncio.create_task(config_manager.watch(interval=0.1))

        # 等待监控启动
        await asyncio.sleep(0.1)

        # 修改文件
        new_data = {"bot_name": "changed"}
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(new_data, f)

        # 等待检测
        await asyncio.sleep(0.3)

        # 取消监控
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass

        assert len(changes_detected) > 0

    @pytest.mark.asyncio
    async def test_watch_detects_change_written_during_callback(
        self,
        config_manager: ConfigManager,
        config_file: Path,
    ):
        """测试回调执行期间写入的新配置不会被 mtime 覆盖掉"""
        import asyncio

        changes_detected = []
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

        async def wait_for_changes(count: int) -> None:
            while len(changes_detected) < count:
                await asyncio.sleep(0.02)

        config_manager.on_reload(callback)
        watch_task = asyncio.create_task(config_manager.watch(interval=0.02))
        await asyncio.sleep(0.05)

        write_config("first", first_mtime)

        try:
            await asyncio.wait_for(wait_for_changes(2), timeout=1.0)
        finally:
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass

        assert changes_detected[:2] == ["first", "second"]

# ============================================================
# ConfigSnapshot 测试
# ============================================================

class TestConfigSnapshot:
    """ConfigSnapshot 数据类测试"""

    def test_create_snapshot(self):
        """测试创建快照"""
        snapshot = ConfigSnapshot(
            config={"key": "value"},
            secrets={"secret": "hidden"}
        )
        assert snapshot.config == {"key": "value"}
        assert snapshot.secrets == {"secret": "hidden"}

# ============================================================
# _check_secrets_file_permissions 测试
# ============================================================

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

# ============================================================
# TestConfigHotReload - 配置热加载和错误处理测试
# ============================================================

class TestConfigHotReload:
    """测试配置热加载功能"""

    @pytest.mark.asyncio
    async def test_config_reload(self, temp_config_dir: Path, config_file: Path, secrets_file: Path):
        """测试配置重新加载"""
        manager = ConfigManager(config_file, secrets_file)
        initial_value = manager.config.get("bot_name")

        # 修改配置文件
        new_config = json.loads(config_file.read_text(encoding="utf-8"))
        new_config["bot_name"] = "新名字"
        config_file.write_text(json.dumps(new_config, ensure_ascii=False, indent=2), encoding="utf-8")

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

        # ConfigManager 使用 load_json，无效 JSON 应该返回空字典而不是抛出异常
        manager = ConfigManager(invalid_config, invalid_secrets)
        # 由于 load_json 对无效 JSON 返回空字典，manager 应该正常初始化
        assert manager.config == {}
        assert manager.secrets == {}

    @pytest.mark.asyncio
    async def test_missing_secrets_fallback(self, temp_config_dir: Path):
        """测试缺失secrets.json的降级处理"""
        config_file = temp_config_dir / "config.json"
        config_data = {"bot_name": "测试机器人"}
        config_file.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 使用不存在的 secrets 文件
        missing_secrets = temp_config_dir / "nonexistent_secrets.json"

        # ConfigManager 应该能处理缺失的 secrets 文件
        manager = ConfigManager(config_file, missing_secrets)
        assert manager.secrets == {}
        assert manager.config.get("bot_name") == "测试机器人"

# ============================================================
# 线程安全测试
# ============================================================

class TestConfigManagerThreadSafety:
    """ConfigManager 线程安全测试"""

    def test_concurrent_reads(self, config_manager: ConfigManager):
        """测试并发读取"""
        import threading

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

def test_plugin_secret_store_creates_reads_and_deletes_scoped_values(config_manager: ConfigManager):
    config_manager.set_plugin_secret("qingssh", "passwords.ref-1", "top-secret")

    assert config_manager.get_plugin_secret("qingssh", "passwords.ref-1") == "top-secret"
    assert config_manager.get_plugin_secret("other", "passwords.ref-1") is None
    assert config_manager.delete_plugin_secret("qingssh", "passwords.ref-1") is True
    assert config_manager.get_plugin_secret("qingssh", "passwords.ref-1") is None


# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
