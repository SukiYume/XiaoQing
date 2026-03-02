"""
日志模块单元测试
"""

import pytest
import logging
from pathlib import Path

from core.logging_config import (
    LogManager,
    ColoredFormatter,
    setup_logging,
    get_log_manager,
    get_logger,
)


# ============================================================
# ColoredFormatter 测试
# ============================================================

class TestColoredFormatter:
    """ColoredFormatter 测试类"""

    def test_format_with_color(self):
        """测试带颜色格式化"""
        formatter = ColoredFormatter(
            fmt="%(levelname)s: %(message)s",
            use_color=True,
        )
        
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        
        result = formatter.format(record)
        # 应包含 ANSI 颜色代码
        assert "\033[32m" in result  # 绿色
        assert "\033[0m" in result   # 重置
        assert "Test message" in result

    def test_format_without_color(self):
        """测试无颜色格式化"""
        formatter = ColoredFormatter(
            fmt="%(levelname)s: %(message)s",
            use_color=False,
        )
        
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        
        result = formatter.format(record)
        # 不应包含 ANSI 颜色代码
        assert "\033[" not in result
        assert "INFO: Test message" in result


# ============================================================
# LogManager 测试
# ============================================================

class TestLogManager:
    """LogManager 测试类"""

    def test_create_log_manager(self, tmp_path: Path):
        """测试创建日志管理器"""
        manager = LogManager(
            log_dir=tmp_path / "logs",
            level="INFO",
            console_output=False,  # 测试时禁用控制台
            file_output=True,
        )
        
        assert manager.level == logging.INFO
        assert (tmp_path / "logs").exists()

    def test_log_file_created(self, tmp_path: Path):
        """测试日志文件创建"""
        log_dir = tmp_path / "logs"
        manager = LogManager(
            log_dir=log_dir,
            level="INFO",
            console_output=False,
            file_output=True,
        )
        
        # 写入日志
        logger = logging.getLogger("test_file_creation")
        logger.info("Test log message")
        
        # 检查文件是否创建
        assert (log_dir / "xiaoqing.log").exists()

    def test_error_log_file(self, tmp_path: Path):
        """测试错误日志文件"""
        log_dir = tmp_path / "logs"
        manager = LogManager(
            log_dir=log_dir,
            level="DEBUG",
            console_output=False,
            file_output=True,
        )
        
        # 写入错误日志
        logger = logging.getLogger("test_error_log")
        logger.error("Test error message")
        
        # 检查错误日志文件
        assert (log_dir / "xiaoqing_error.log").exists()
        
        # 读取并验证内容
        content = (log_dir / "xiaoqing_error.log").read_text(encoding="utf-8")
        assert "Test error message" in content

    def test_set_level(self, tmp_path: Path):
        """测试动态设置日志级别"""
        manager = LogManager(
            log_dir=tmp_path / "logs",
            level="INFO",
            console_output=False,
            file_output=False,
        )
        
        assert manager.level == logging.INFO
        
        manager.set_level("DEBUG")
        assert manager.level == logging.DEBUG
        
        manager.set_level("WARNING")
        assert manager.level == logging.WARNING

    def test_rotation_type_size(self, tmp_path: Path):
        """测试按大小轮转"""
        manager = LogManager(
            log_dir=tmp_path / "logs",
            level="INFO",
            console_output=False,
            file_output=True,
            rotation_type="size",
            max_bytes=1024,  # 1KB 便于测试
        )
        
        assert manager.rotation_type == "size"

    def test_rotation_type_time(self, tmp_path: Path):
        """测试按时间轮转"""
        manager = LogManager(
            log_dir=tmp_path / "logs",
            level="INFO",
            console_output=False,
            file_output=True,
            rotation_type="time",
        )
        
        assert manager.rotation_type == "time"


# ============================================================
# setup_logging 测试
# ============================================================

class TestSetupLogging:
    """setup_logging 函数测试"""

    def test_setup_with_defaults(self, tmp_path: Path):
        """测试使用默认配置"""
        config = {}
        manager = setup_logging(config, log_dir=tmp_path / "logs")
        
        assert manager is not None
        assert manager.level == logging.INFO

    def test_setup_with_custom_config(self, tmp_path: Path):
        """测试使用自定义配置"""
        config = {
            "log_level": "DEBUG",
            "log_to_file": True,
            "log_to_console": False,
            "log_use_color": False,
            "log_max_size_mb": 5,
            "log_backup_count": 3,
            "log_rotation": "size",
        }
        manager = setup_logging(config, log_dir=tmp_path / "logs")
        
        assert manager.level == logging.DEBUG
        assert manager.file_output is True
        assert manager.console_output is False
        assert manager.rotation_type == "size"

    def test_get_log_manager_after_setup(self, tmp_path: Path):
        """测试 setup 后获取 manager"""
        config = {"log_level": "INFO"}
        setup_logging(config, log_dir=tmp_path / "logs")
        
        manager = get_log_manager()
        assert manager is not None


# ============================================================
# get_logger 测试
# ============================================================

class TestGetLogger:
    """get_logger 函数测试"""

    def test_get_named_logger(self):
        """测试获取命名 logger"""
        logger1 = get_logger("my.module")
        logger2 = get_logger("my.module")
        
        assert logger1 is logger2
        assert logger1.name == "my.module"

    def test_logger_hierarchy(self):
        """测试 logger 层级"""
        parent = get_logger("parent")
        child = get_logger("parent.child")
        
        assert child.parent.name == "parent"


# ============================================================
# 运行测试
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
