"""
XiaoQing 核心模块

提供机器人核心功能：
- app: 主应用入口
- args: 参数解析
- plugin_base: 插件基础工具（消息段、异步工具、文件工具）
- config: 配置管理
- context: 插件上下文
- plugin_manager: 插件管理
- scheduler: 定时任务
- dispatcher: 消息分发
- router: 命令路由
- server: HTTP/WebSocket 服务
- onebot: OneBot 协议支持
- session: 会话管理（多轮对话支持）
- exceptions: 自定义异常
- metrics: 性能监控

"""
from .config import ConfigManager, ConfigSnapshot
from .router import CommandRouter, CommandSpec
from .dispatcher import Dispatcher
from .plugin_manager import PluginManager, LoadedPlugin

# 参数解析
from .args import tokenize, parse, parse_kv, ParsedArgs

# 插件基础工具
from .plugin_base import (
    segments,
    build_action,
    text,
    image,
    image_url,
    run_sync,
    ensure_dir,
    load_json,
    write_json,
)

# 日志配置
from .logging_config import setup_logging, get_logger, get_log_manager, LogManager

# 会话管理
from .session import Session, SessionManager

# 异常
from .exceptions import (
    XiaoQingError,
    PluginError,
    PluginLoadError,
    PluginExecutionError,
    PluginTimeoutError,
    CommandError,
    CommandNotFoundError,
    CommandPermissionError,
    CommandArgumentError,
    ConfigError,
    SessionError,
)

# 性能监控
from .metrics import (
    MetricsCollector,
    ExecutionStats,
    ExecutionTimer,
    get_metrics_collector,
    set_metrics_collector,
)

__all__ = [
    # 参数解析
    "tokenize",
    "parse",
    "parse_kv",
    "ParsedArgs",
    # 消息段
    "segments",
    "build_action",
    "text",
    "image",
    "image_url",
    # 异步工具
    "run_sync",
    # 文件工具
    "ensure_dir",
    "load_json",
    "write_json",
    # 日志
    "setup_logging",
    "get_logger",
    "get_log_manager",
    "LogManager",
    # 会话管理
    "Session",
    "SessionManager",
    # 异常
    "XiaoQingError",
    "PluginError",
    "PluginLoadError",
    "PluginExecutionError",
    "PluginTimeoutError",
    "CommandError",
    "CommandNotFoundError",
    "CommandPermissionError",
    "CommandArgumentError",
    "ConfigError",
    "SessionError",
    # 性能监控
    "MetricsCollector",
    "ExecutionStats",
    "ExecutionTimer",
    "get_metrics_collector",
    "set_metrics_collector",
    # 核心组件
    "ConfigManager",
    "ConfigSnapshot",
    "CommandRouter",
    "CommandSpec",
    "Dispatcher",
    "PluginManager",
    "LoadedPlugin",
]
