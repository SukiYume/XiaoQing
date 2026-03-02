"""
日志管理模块

提供完整的日志管理功能：
- 控制台输出（带颜色）
- 文件保存（按日期轮转）
- 日志级别配置
- 统一格式化
"""

import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Any

# ============================================================
# 颜色格式化（控制台）
# ============================================================

class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式化器（仅用于控制台）"""
    
    # ANSI 颜色代码
    COLORS = {
        'DEBUG': '\033[36m',     # 青色
        'INFO': '\033[32m',      # 绿色
        'WARNING': '\033[33m',   # 黄色
        'ERROR': '\033[31m',     # 红色
        'CRITICAL': '\033[35m',  # 紫色
    }
    RESET = '\033[0m'
    
    def __init__(self, fmt: str = None, datefmt: str = None, use_color: bool = True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color
    
    def format(self, record: logging.LogRecord) -> str:
        # 保存原始 levelname
        original_levelname = record.levelname
        
        if self.use_color and record.levelname in self.COLORS:
            # 添加颜色
            record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
        
        result = super().format(record)
        
        # 恢复原始 levelname
        record.levelname = original_levelname
        
        return result

# ============================================================
# 日志管理器
# ============================================================

class LogManager:
    """
    日志管理器
    
    支持功能：
    - 控制台输出（可配置颜色）
    - 文件输出（按大小或时间轮转）
    - 多个日志文件（主日志、错误日志）
    - 动态调整日志级别
    """
    
    # 默认格式
    DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
    
    # 文件格式（更详细，包含文件名和行号）
    FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
    
    def __init__(
        self,
        log_dir: Path,
        level: str = "INFO",
        console_output: bool = True,
        file_output: bool = True,
        use_color: bool = True,
        max_bytes: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 5,
        rotation_type: str = "size",  # "size" 或 "time"
    ):
        self.log_dir = Path(log_dir)
        self.level = getattr(logging, level.upper(), logging.INFO)
        self.console_output = console_output
        self.file_output = file_output
        self.use_color = use_color
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.rotation_type = rotation_type
        
        # 确保日志目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 保存 handler 引用，方便后续管理
        self._console_handler: logging.Handler | None = None
        self._file_handler: logging.Handler | None = None
        self._error_handler: logging.Handler | None = None
        
        # 初始化
        self._setup()
    
    def _setup(self) -> None:
        """设置日志系统"""
        # 获取根 logger
        root_logger = logging.getLogger()
        root_logger.setLevel(self.level)
        
        # 清除现有 handlers
        root_logger.handlers.clear()
        
        # 添加控制台 handler
        if self.console_output:
            self._console_handler = self._create_console_handler()
            root_logger.addHandler(self._console_handler)
        
        # 添加文件 handler
        if self.file_output:
            self._file_handler = self._create_file_handler()
            root_logger.addHandler(self._file_handler)
            
            # 添加错误日志文件（仅记录 ERROR 及以上）
            self._error_handler = self._create_error_handler()
            root_logger.addHandler(self._error_handler)
        
        # 降低第三方库的日志级别
        logging.getLogger("apscheduler").setLevel(logging.WARNING)
        logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
        logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    
    def _create_console_handler(self) -> logging.Handler:
        """创建控制台 handler"""
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(self.level)
        
        # 检测是否应该使用颜色
        use_color = self.use_color
        
        # 如果 stdout 不是终端（被重定向到文件），禁用颜色
        if not sys.stdout.isatty():
            use_color = False
        elif sys.platform == "win32":
            # Windows 尝试启用 ANSI 支持
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                use_color = False
        
        formatter = ColoredFormatter(
            fmt=self.DEFAULT_FORMAT,
            datefmt=self.DEFAULT_DATE_FORMAT,
            use_color=use_color,
        )
        handler.setFormatter(formatter)
        
        return handler
    
    def _create_file_handler(self) -> logging.Handler:
        """创建文件 handler（按大小或时间轮转）"""
        log_file = self.log_dir / "xiaoqing.log"
        
        if self.rotation_type == "time":
            # 按时间轮转（每天一个文件）
            handler = TimedRotatingFileHandler(
                filename=str(log_file),
                when="midnight",
                interval=1,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            # 设置文件名后缀格式
            handler.suffix = "%Y-%m-%d"
        else:
            # 按大小轮转
            handler = RotatingFileHandler(
                filename=str(log_file),
                maxBytes=self.max_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
        
        handler.setLevel(self.level)
        formatter = logging.Formatter(
            fmt=self.FILE_FORMAT,
            datefmt=self.DEFAULT_DATE_FORMAT,
        )
        handler.setFormatter(formatter)
        
        return handler
    
    def _create_error_handler(self) -> logging.Handler:
        """创建错误日志 handler（仅记录 ERROR 及以上）"""
        error_file = self.log_dir / "xiaoqing_error.log"
        
        handler = RotatingFileHandler(
            filename=str(error_file),
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding="utf-8",
        )
        handler.setLevel(logging.ERROR)
        
        formatter = logging.Formatter(
            fmt=self.FILE_FORMAT,
            datefmt=self.DEFAULT_DATE_FORMAT,
        )
        handler.setFormatter(formatter)
        
        return handler
    
    def set_level(self, level: str) -> None:
        """动态设置日志级别"""
        self.level = getattr(logging, level.upper(), logging.INFO)
        
        root_logger = logging.getLogger()
        root_logger.setLevel(self.level)
        
        if self._console_handler:
            self._console_handler.setLevel(self.level)
        if self._file_handler:
            self._file_handler.setLevel(self.level)
    
    def get_logger(self, name: str) -> logging.Logger:
        """获取指定名称的 logger"""
        return get_logger(name)

# ============================================================
# 便捷函数
# ============================================================

_log_manager: LogManager | None = None

def setup_logging(config: dict[str, Any], log_dir: Path | None = None) -> LogManager:
    """
    设置日志系统
    
    Args:
        config: 配置字典，支持以下键：
            - log_level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
            - log_to_file: 是否保存到文件 (默认 True)
            - log_to_console: 是否输出到控制台 (默认 True)
            - log_use_color: 是否使用颜色 (默认 True)
            - log_max_size_mb: 单个日志文件最大大小 MB (默认 10)
            - log_backup_count: 保留的日志文件数量 (默认 5)
            - log_rotation: 轮转方式 "size" 或 "time" (默认 "time")
        log_dir: 日志目录，默认为项目根目录下的 logs 文件夹
    
    Returns:
        LogManager 实例
    """
    global _log_manager
    
    # 从配置读取参数
    level = config.get("log_level", "INFO")
    file_output = config.get("log_to_file", True)
    console_output = config.get("log_to_console", True)
    use_color = config.get("log_use_color", True)
    max_size_mb = config.get("log_max_size_mb", 10)
    backup_count = config.get("log_backup_count", 5)
    rotation_type = config.get("log_rotation", "time")
    
    # 默认日志目录
    if log_dir is None:
        log_dir = Path(__file__).parent.parent / "logs"
    
    _log_manager = LogManager(
        log_dir=log_dir,
        level=level,
        console_output=console_output,
        file_output=file_output,
        use_color=use_color,
        max_bytes=max_size_mb * 1024 * 1024,
        backup_count=backup_count,
        rotation_type=rotation_type,
    )
    
    # 记录启动信息
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("XiaoQing 日志系统初始化完成")
    logger.info("日志级别: %s", level)
    logger.info("日志目录: %s", log_dir)
    logger.info("轮转方式: %s", rotation_type)
    logger.info("=" * 60)
    
    return _log_manager

def get_log_manager() -> LogManager | None:
    """获取全局 LogManager 实例"""
    return _log_manager

def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger"""
    return logging.getLogger(name)

__all__ = [
    "LogManager",
    "ColoredFormatter",
    "setup_logging",
    "get_log_manager",
    "get_logger",
]
