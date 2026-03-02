"""
XiaoQing 异常定义

自定义异常类，用于更精确的错误处理和调试。
"""

from typing import Optional


class XiaoQingError(Exception):
    """XiaoQing 框架基础异常"""
    pass


# ============================================================
# 插件相关异常
# ============================================================

class PluginError(XiaoQingError):
    """插件执行错误"""
    
    def __init__(self, plugin_name: str, message: str, cause: Optional[Exception] = None):
        self.plugin_name = plugin_name
        self.cause = cause
        super().__init__(f"[{plugin_name}] {message}")


class PluginLoadError(PluginError):
    """插件加载错误"""
    pass


class PluginExecutionError(PluginError):
    """插件执行错误"""
    pass


class PluginTimeoutError(PluginError):
    """插件执行超时"""
    pass


# ============================================================
# 命令相关异常
# ============================================================

class CommandError(XiaoQingError):
    """命令错误"""
    pass


class CommandNotFoundError(CommandError):
    """命令未找到"""
    
    def __init__(self, command: str):
        self.command = command
        super().__init__(f"Command not found: {command}")


class CommandPermissionError(CommandError):
    """命令权限不足"""
    
    def __init__(self, command: str, user_id: int):
        self.command = command
        self.user_id = user_id
        super().__init__(f"Permission denied for command '{command}' by user {user_id}")


class CommandArgumentError(CommandError):
    """命令参数错误"""
    
    def __init__(self, command: str, message: str):
        self.command = command
        super().__init__(f"Invalid arguments for '{command}': {message}")


# ============================================================
# 配置相关异常
# ============================================================

class ConfigError(XiaoQingError):
    """配置错误"""
    pass


class ConfigLoadError(ConfigError):
    """配置加载错误"""
    pass


class ConfigValidationError(ConfigError):
    """配置校验错误"""
    pass


# ============================================================
# 会话相关异常
# ============================================================

class SessionError(XiaoQingError):
    """会话错误"""
    pass


class SessionNotFoundError(SessionError):
    """会话不存在"""
    
    def __init__(self, user_id: int, group_id: Optional[int] = None):
        self.user_id = user_id
        self.group_id = group_id
        location = f"group {group_id}" if group_id else "private"
        super().__init__(f"No active session for user {user_id} in {location}")


class SessionExpiredError(SessionError):
    """会话已过期"""
    pass


# ============================================================
# 通信相关异常
# ============================================================

class CommunicationError(XiaoQingError):
    """通信错误"""
    pass


class OneBotError(CommunicationError):
    """OneBot 通信错误"""
    pass


class AuthenticationError(CommunicationError):
    """认证错误"""
    pass


__all__ = [
    # 基础
    "XiaoQingError",
    # 插件
    "PluginError",
    "PluginLoadError",
    "PluginExecutionError",
    "PluginTimeoutError",
    # 命令
    "CommandError",
    "CommandNotFoundError",
    "CommandPermissionError",
    "CommandArgumentError",
    # 配置
    "ConfigError",
    "ConfigLoadError",
    "ConfigValidationError",
    # 会话
    "SessionError",
    "SessionNotFoundError",
    "SessionExpiredError",
    # 通信
    "CommunicationError",
    "OneBotError",
    "AuthenticationError",
]
