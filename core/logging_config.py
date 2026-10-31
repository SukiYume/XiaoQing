"""日志管理：彩色控制台输出、按大小或日期轮转的主日志与错误日志，统一带 request_id 的格式。"""

import copy
import logging
import sys
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any, ClassVar

_STD_OUTPUT_HANDLE                  = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _enable_windows_ansi(kernel32: Any) -> bool:
    """Enable ANSI output without discarding the console's existing mode bits."""

    import ctypes

    handle       = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    current_mode = ctypes.c_uint()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(current_mode)):
        return False
    updated_mode = current_mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
    return bool(kernel32.SetConsoleMode(handle, updated_mode))


### 没有 request_id 的后台日志统一显示为 "-"
class RequestContextFormatter(logging.Formatter):
    """Formatter that makes request correlation visible without breaking background logs."""

    def format(self, record: logging.LogRecord) -> str:
        # Do not mutate the shared record: one handler may receive an explicit
        # request ID while another formatter still needs the original object.
        safe_record = copy.copy(record)
        if not getattr(safe_record, "request_id", None):
            safe_record.request_id = "-"
        return super().format(safe_record)


class ColoredFormatter(RequestContextFormatter):
    """带颜色的日志格式化器（仅用于控制台）"""

    # ANSI 颜色代码
    COLORS: ClassVar[dict[str, str]] = {
        "DEBUG"   : "\033[36m",  # 青色
        "INFO"    : "\033[32m",  # 绿色
        "WARNING" : "\033[33m",  # 黄色
        "ERROR"   : "\033[31m",  # 红色
        "CRITICAL": "\033[35m",  # 紫色
    }
    RESET = "\033[0m"

    def __init__(
        self,
        fmt: str | None     = None,
        datefmt: str | None = None,
        use_color: bool     = True,
    ) -> None:
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if self.use_color and record.levelname in self.COLORS:
            colored_record           = copy.copy(record)
            colored_record.levelname = (
                f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
            )
            return super().format(colored_record)

        return super().format(record)


class LogManager:
    """创建时即接管根 logger：控制台（可选颜色）+ 主日志文件 + 错误日志文件。"""

    # 默认格式
    DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] [request_id=%(request_id)s] %(name)s: %(message)s"
    DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    # 文件格式（更详细，包含文件名和行号）
    FILE_FORMAT = (
        "%(asctime)s [%(levelname)s] [request_id=%(request_id)s] "
        "%(name)s (%(filename)s:%(lineno)d): %(message)s"
    )

    def __init__(
        self,
        log_dir: Path,
        level: str           = "INFO",
        console_output: bool = True,
        file_output: bool    = True,
        use_color: bool      = True,
        max_bytes: int       = 10 * 1024 * 1024,  # 10MB
        backup_count: int    = 5,
        rotation_type: str   = "size",  # "size" 或 "time"
    ):
        self.log_dir        = Path(log_dir)
        self.level          = getattr(logging, level.upper(), logging.INFO)
        self.console_output = console_output
        self.file_output    = file_output
        self.use_color      = use_color
        self.max_bytes      = max_bytes
        self.backup_count   = backup_count
        self.rotation_type  = rotation_type

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._setup()

    def _setup(self) -> None:
        """替换根 logger 的全部 handler：控制台、主日志文件、错误日志文件。"""

        root_logger = logging.getLogger()
        root_logger.setLevel(self.level)

        # 清除现有 handlers；pytest 的捕获 handler 只摘除不关闭
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            if handler.__class__.__module__.startswith("_pytest."):
                continue
            for close_step in (handler.flush, handler.close):
                try:
                    close_step()
                except Exception:
                    pass

        if self.console_output:
            root_logger.addHandler(self._create_console_handler())
        if self.file_output:
            root_logger.addHandler(
                self._create_file_handler("xiaoqing.log", self.level, self.rotation_type)
            )
            # 错误日志只记录 ERROR 及以上，始终按大小轮转
            root_logger.addHandler(
                self._create_file_handler("xiaoqing_error.log", logging.ERROR, "size")
            )

        # 降低第三方库的日志级别
        for name in ("apscheduler", "apscheduler.executors.default", "apscheduler.scheduler"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def _create_console_handler(self) -> logging.Handler:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(self.level)

        # stdout 被重定向到文件时禁用颜色；Windows 需先开启 ANSI 支持
        use_color = self.use_color
        if not sys.stdout.isatty():
            use_color = False
        elif sys.platform == "win32":
            try:
                import ctypes

                use_color = _enable_windows_ansi(ctypes.windll.kernel32)
            except Exception:
                use_color = False

        handler.setFormatter(
            ColoredFormatter(
                fmt=self.DEFAULT_FORMAT, datefmt=self.DEFAULT_DATE_FORMAT, use_color=use_color
            )
        )
        return handler

    def _create_file_handler(
        self, filename: str, level: int, rotation_type: str
    ) -> logging.Handler:
        """创建文件 handler：time 为每天午夜轮转，其余按大小轮转。"""

        log_file = str(self.log_dir / filename)
        handler: logging.Handler
        if rotation_type == "time":
            timed_handler = TimedRotatingFileHandler(
                filename    = log_file,
                when        = "midnight",
                interval    = 1,
                backupCount = self.backup_count,
                encoding    = "utf-8",
            )
            timed_handler.suffix = "%Y-%m-%d"
            handler              = timed_handler
        else:
            handler = RotatingFileHandler(
                filename    = log_file,
                maxBytes    = self.max_bytes,
                backupCount = self.backup_count,
                encoding    = "utf-8",
            )
        handler.setLevel(level)
        handler.setFormatter(
            RequestContextFormatter(fmt=self.FILE_FORMAT, datefmt=self.DEFAULT_DATE_FORMAT)
        )
        return handler


def setup_logging(config: Mapping[str, Any], log_dir: Path | None = None) -> LogManager:
    """
    按配置初始化日志系统，支持以下键：
        - log_level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        - log_to_file: 是否保存到文件 (默认 True)
        - log_to_console: 是否输出到控制台 (默认 True)
        - log_use_color: 是否使用颜色 (默认 True)
        - log_max_size_mb: 单个日志文件最大大小 MB (默认 10)
        - log_backup_count: 保留的日志文件数量 (默认 5)
        - log_rotation: 轮转方式 "size" 或 "time" (默认 "time")
    log_dir 默认为项目根目录下的 logs 文件夹。
    """

    level         = config.get("log_level", "INFO")
    rotation_type = config.get("log_rotation", "time")
    log_dir       = log_dir or Path(__file__).parent.parent / "logs"
    manager       = LogManager(
        log_dir        = log_dir,
        level          = level,
        console_output = config.get("log_to_console", True),
        file_output    = config.get("log_to_file", True),
        use_color      = config.get("log_use_color", True),
        max_bytes      = config.get("log_max_size_mb", 10) * 1024 * 1024,
        backup_count   = config.get("log_backup_count", 5),
        rotation_type  = rotation_type,
    )

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("XiaoQing 日志系统初始化完成")
    logger.info("日志级别: %s", level)
    logger.info("日志目录: %s", log_dir)
    logger.info("轮转方式: %s", rotation_type)
    logger.info("=" * 60)
    return manager


__all__ = ["ColoredFormatter", "LogManager", "RequestContextFormatter", "setup_logging"]
