"""真实加载的测试插件把观察结果交回测试的记录器。

测试插件的源码写在插件目录里，由 Core 按生产路径导入；源码里 ``from tests.helpers.plugin_probe
import PROBE`` 拿到的是和测试同一个进程内对象。每条用例开始前由 ``reset`` 清空。
"""

from typing import Any


class PluginProbe:
    """按钩子名收录调用、预设返回值，以及需要在测试与插件之间共享的对象。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._logs: dict[str, list[Any]]     = {}
        self._scripted: dict[str, list[Any]] = {}
        self.slots: dict[str, Any]           = {}

    def log(self, hook: str) -> list[Any]:
        """``hook`` 的记录列表（实时的同一个列表）：插件往里追加，测试读取。"""

        return self._logs.setdefault(hook, [])

    def script(self, hook: str, *results: Any) -> None:
        """预设 ``hook`` 依次返回的结果；用完后 ``next_result`` 返回 ``None``。"""

        self._scripted[hook] = list(results)

    def next_result(self, hook: str) -> Any:
        queued = self._scripted.get(hook)
        return queued.pop(0) if queued else None


PROBE = PluginProbe()
