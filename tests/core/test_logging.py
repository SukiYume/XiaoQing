"""
日志模块单元测试
"""

import logging
from pathlib import Path

from core.logging_config import (
    ColoredFormatter,
    LogManager,
    RequestContextFormatter,
    _enable_windows_ansi,
    setup_logging,
)


class _FakeKernel32:
    def __init__(self, *, mode: int = 0x20, get_mode_ok: bool = True) -> None:
        self.mode                             = mode
        self.get_mode_ok                      = get_mode_ok
        self.set_calls: list[tuple[int, int]] = []

    def GetStdHandle(self, identifier: int) -> int:
        assert identifier == -11
        return 123

    def GetConsoleMode(self, handle: int, mode_pointer) -> int:
        assert handle == 123
        if not self.get_mode_ok:
            return 0
        mode_pointer._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle: int, mode: int) -> int:
        self.set_calls.append((handle, mode))
        return 1


# ============================================================
# ColoredFormatter 测试
# ============================================================


class TestColoredFormatter:
    """ColoredFormatter 测试类"""

    def test_format_with_color(self):
        """测试带颜色格式化"""
        formatter = ColoredFormatter(
            fmt       = "%(levelname)s: %(message)s",
            use_color = True,
        )

        record = logging.LogRecord(
            name     = "test",
            level    = logging.INFO,
            pathname = "",
            lineno   = 0,
            msg      = "Test message",
            args     = (),
            exc_info = None,
        )

        result = formatter.format(record)
        # 应包含 ANSI 颜色代码
        assert "\033[32m" in result  # 绿色
        assert "\033[0m" in result  # 重置
        assert "Test message" in result
        assert record.levelname == "INFO"

    def test_format_with_color_does_not_mutate_record(self):
        """测试带颜色格式化不会修改原始 LogRecord"""
        formatter = ColoredFormatter(
            fmt       = "%(levelname)s: %(message)s",
            use_color = True,
        )
        record = logging.LogRecord(
            name     = "test",
            level    = logging.WARNING,
            pathname = "",
            lineno   = 0,
            msg      = "Test message",
            args     = (),
            exc_info = None,
        )

        formatter.format(record)

        assert record.levelname == "WARNING"

    def test_format_without_color(self):
        """测试无颜色格式化"""
        formatter = ColoredFormatter(
            fmt       = "%(levelname)s: %(message)s",
            use_color = False,
        )

        record = logging.LogRecord(
            name     = "test",
            level    = logging.INFO,
            pathname = "",
            lineno   = 0,
            msg      = "Test message",
            args     = (),
            exc_info = None,
        )

        result = formatter.format(record)
        # 不应包含 ANSI 颜色代码
        assert "\033[" not in result
        assert "INFO: Test message" in result

    def test_windows_ansi_preserves_existing_console_mode_bits(self):
        kernel32 = _FakeKernel32(mode=0x23)

        assert _enable_windows_ansi(kernel32) is True
        assert kernel32.set_calls == [(123, 0x27)]

    def test_windows_ansi_fails_closed_when_mode_cannot_be_read(self):
        kernel32 = _FakeKernel32(get_mode_ok=False)

        assert _enable_windows_ansi(kernel32) is False
        assert kernel32.set_calls == []

    def test_format_includes_request_id_or_safe_background_placeholder(self):
        formatter      = RequestContextFormatter("[request_id=%(request_id)s] %(message)s")
        request_record = logging.LogRecord(
            name     = "test",
            level    = logging.INFO,
            pathname = "",
            lineno   = 0,
            msg      = "correlated",
            args     = (),
            exc_info = None,
        )
        request_record.request_id = "req-123"  # type: ignore[attr-defined]
        background_record         = logging.LogRecord(
            name     = "test",
            level    = logging.INFO,
            pathname = "",
            lineno   = 0,
            msg      = "background",
            args     = (),
            exc_info = None,
        )

        assert formatter.format(request_record) == "[request_id=req-123] correlated"
        assert formatter.format(background_record) == "[request_id=-] background"
        assert not hasattr(background_record, "request_id")


# ============================================================
# LogManager 测试
# ============================================================


class TestLogManager:
    """LogManager 测试类"""

    def test_create_log_manager(self, tmp_path: Path):
        """测试创建日志管理器"""
        manager = LogManager(
            log_dir        = tmp_path / "logs",
            level          = "INFO",
            console_output = False,  # 测试时禁用控制台
            file_output    = True,
        )

        assert manager.level == logging.INFO
        assert (tmp_path / "logs").exists()

    def test_log_file_created(self, tmp_path: Path):
        """测试日志文件创建"""
        log_dir  = tmp_path / "logs"
        _manager = LogManager(
            log_dir        = log_dir,
            level          = "INFO",
            console_output = False,
            file_output    = True,
        )

        # 写入日志
        logger = logging.getLogger("test_file_creation")
        logger.info("Test log message")

        # 检查文件是否创建
        assert (log_dir / "xiaoqing.log").exists()

    def test_error_log_file(self, tmp_path: Path):
        """测试错误日志文件"""
        log_dir  = tmp_path / "logs"
        _manager = LogManager(
            log_dir        = log_dir,
            level          = "DEBUG",
            console_output = False,
            file_output    = True,
        )

        # 写入错误日志
        logger = logging.getLogger("test_error_log")
        logger.error("Test error message")

        # 检查错误日志文件
        assert (log_dir / "xiaoqing_error.log").exists()

        # 读取并验证内容
        content = (log_dir / "xiaoqing_error.log").read_text(encoding="utf-8")
        assert "Test error message" in content

    def test_file_logs_include_request_id(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        LogManager(log_dir=log_dir, level="INFO", console_output=False, file_output=True)
        logger = logging.getLogger("test_request_id_file")
        logger.info("correlated message", extra={"request_id": "req-file-1"})
        logger.info("background message")

        content = (log_dir / "xiaoqing.log").read_text(encoding="utf-8")
        assert "[request_id=req-file-1]" in content
        assert "[request_id=-]" in content

    def test_rotation_type_size(self, tmp_path: Path):
        """实际写入触发大小轮转，历史文件数量受配置约束。"""
        from logging.handlers import RotatingFileHandler

        manager = LogManager(
            log_dir        = tmp_path / "logs",
            level          = "INFO",
            console_output = False,
            file_output    = True,
            rotation_type  = "size",
            max_bytes      = 256,
            backup_count   = 2,
        )
        handler = next(
            h
            for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).name == "xiaoqing.log"
        )
        try:
            for index in range(6):
                handler.handle(
                    logging.LogRecord(
                        "rotation",
                        logging.INFO,
                        __file__,
                        1,
                        f"entry-{index}-" + "x" * 200,
                        (),
                        None,
                    )
                )
            handler.flush()
            assert len(list(manager.log_dir.glob("xiaoqing.log.*"))) == 2
            assert "entry-5" in (manager.log_dir / "xiaoqing.log").read_text(encoding="utf-8")
            assert "entry-3" in (manager.log_dir / "xiaoqing.log.2").read_text(encoding="utf-8")
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()

    def test_rotation_type_time(self, tmp_path: Path):
        """推进轮转截止点，验证按日期归档及历史保留数量。"""
        import time
        from logging.handlers import TimedRotatingFileHandler

        manager = LogManager(
            log_dir        = tmp_path / "logs",
            level          = "INFO",
            console_output = False,
            file_output    = True,
            rotation_type  = "time",
            backup_count   = 2,
        )
        handler = next(
            h for h in logging.getLogger().handlers if isinstance(h, TimedRotatingFileHandler)
        )
        try:
            for index in range(4):
                handler.rolloverAt = int(time.time()) - (5 - index) * 86400
                handler.handle(
                    logging.LogRecord(
                        "rotation", logging.INFO, __file__, 1, f"day-{index}", (), None
                    )
                )
            handler.flush()
            archived = list(manager.log_dir.glob("xiaoqing.log.*"))
            assert len(archived) == 2
            text = "".join(path.read_text(encoding="utf-8") for path in archived)
            assert "day-1" in text and "day-2" in text
            assert "day-3" in (manager.log_dir / "xiaoqing.log").read_text(encoding="utf-8")
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()


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
            "log_level"       : "DEBUG",
            "log_to_file"     : True,
            "log_to_console"  : False,
            "log_use_color"   : False,
            "log_max_size_mb" : 5,
            "log_backup_count": 3,
            "log_rotation"    : "size",
        }
        manager = setup_logging(config, log_dir=tmp_path / "logs")

        assert manager.level == logging.DEBUG
        assert manager.file_output is True
        assert manager.console_output is False
        assert manager.rotation_type == "size"

    def test_setup_closes_existing_handlers(self, tmp_path: Path):
        """测试 setup_logging 会关闭旧 handlers"""
        root_logger = logging.getLogger()

        class DummyHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.was_closed = False

            def emit(self, record):
                return None

            def close(self):
                self.was_closed = True
                super().close()

        handler = DummyHandler()
        root_logger.addHandler(handler)

        try:
            setup_logging({}, log_dir=tmp_path / "logs")
            assert handler.was_closed is True
            assert handler not in root_logger.handlers
        finally:
            if handler in root_logger.handlers:
                root_logger.removeHandler(handler)
            handler.close()
