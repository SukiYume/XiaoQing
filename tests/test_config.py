"""
ConfigManager 单元测试
"""

import json
import logging
import platform
import pytest
import time
from pathlib import Path
from typing import Any

from core.config import ConfigManager, ConfigSnapshot, _check_secrets_file_permissions

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

    def test_returns_copy_of_config(self, config_manager: ConfigManager):
        """测试返回配置的副本（不影响内部状态）"""
        config1 = config_manager.config
        config2 = config_manager.config
        assert config1 is not config2
        assert config1 == config2

    def test_returns_copy_of_secrets(self, config_manager: ConfigManager):
        """测试返回密钥的副本"""
        secrets1 = config_manager.secrets
        secrets2 = config_manager.secrets
        assert secrets1 is not secrets2
        assert secrets1 == secrets2

    def test_config_nested_mutation_does_not_affect_internal_state(self, config_manager: ConfigManager):
        config = config_manager.config
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

# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
