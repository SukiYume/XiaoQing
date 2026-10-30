"""隔离 arXiv 测试注入的可选依赖及随后导入的模块。"""

import sys
from types import ModuleType

import pytest


@pytest.fixture(autouse=True)
def restore_arxiv_import_state():
    prefix     = "plugins.arxiv_filter"
    previous   = {name: module for name, module in sys.modules.items() if name.startswith(prefix)}
    attributes = {
        name: {
            key: value
            for key, value in vars(module).items()
            if isinstance(value, ModuleType) and value.__name__.startswith(prefix)
        }
        for name, module in previous.items()
        if module is not None
    }
    yield
    for name in list(sys.modules):
        if name.startswith(prefix) and name not in previous:
            del sys.modules[name]
    sys.modules.update(previous)
    for name, saved in attributes.items():
        module = previous[name]
        for key, value in list(vars(module).items()):
            if (
                isinstance(value, ModuleType)
                and value.__name__.startswith(prefix)
                and key not in saved
            ):
                delattr(module, key)
        for key, value in saved.items():
            setattr(module, key, value)
