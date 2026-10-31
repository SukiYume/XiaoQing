"""XiaoQing 框架异常：插件加载与配置读取失败时携带上下文信息。"""

from pathlib import Path


### 框架基础异常，所有自定义异常的共同父类
class XiaoQingError(Exception):
    """XiaoQing 框架基础异常"""


### 插件异常，消息前缀带上插件名，便于定位
class PluginError(XiaoQingError):
    """插件执行错误"""

    def __init__(self, plugin_name: str, message: str, cause: Exception | None = None):
        self.plugin_name = plugin_name
        self.cause       = cause
        super().__init__(f"[{plugin_name}] {message}")


class PluginLoadError(PluginError):
    """插件加载错误"""


### 用普通异常承载插件生命周期中的致命 BaseException，避免直接打断事件循环
class PluginLifecycleFatalError(PluginError):
    """Task-safe carrier for a fatal plugin lifecycle BaseException."""

    def __init__(self, plugin_name: str, original: BaseException):
        self.original = original
        super().__init__(
            plugin_name, f"fatal lifecycle error: {type(original).__name__}: {original}"
        )


class ConfigError(XiaoQingError):
    """配置错误"""


### 配置加载失败；传入 original 时第一个参数视为文件路径
class ConfigLoadError(ConfigError):
    """配置加载错误"""

    def __init__(self, message_or_path: str | Path, original: Exception | None = None):
        self.path: Path | None = Path(message_or_path) if original is not None else None
        self.original          = original
        if original is None:
            message = str(message_or_path)
        else:
            message = f"Failed to load config from {self.path}: {original}"
        super().__init__(message)


__all__ = [
    "ConfigError",
    "ConfigLoadError",
    "PluginError",
    "PluginLifecycleFatalError",
    "PluginLoadError",
    "XiaoQingError",
]
