"""Pendo 浏览器测试的源码定位，允许格式器调整空白与赋值对齐。"""

import re

from tests.helpers.paths import REPOSITORY_ROOT

UTILS = REPOSITORY_ROOT / "plugins/pendo/web/static/js/utils"


def replace_js_source(source: str, original: str, replacement: str) -> str:
    """按非空白文本定位唯一片段，保留其余真实 JavaScript 实现。"""
    pattern = r"\s+".join(re.escape(part) for part in original.split())
    updated, count = re.subn(pattern, lambda _match: replacement, source)
    assert count == 1, f"Expected one JavaScript source match, found {count}: {original}"
    return updated


def _inline_module(name: str) -> str:
    return (UTILS / name).read_text(encoding="utf-8").replace("export function", "function")


def palette_runtime() -> str:
    """配色模块没有依赖，页面测试直接内联它的真实源码。"""
    return _inline_module("palette.js")


def icons_runtime() -> str:
    """图标模块没有依赖，页面测试直接内联它的真实源码。"""
    return _inline_module("icons.js")
