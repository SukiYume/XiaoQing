"""Pendo 浏览器测试的源码定位，允许格式器调整空白与赋值对齐。"""

import re


def replace_js_source(source: str, original: str, replacement: str) -> str:
    """按非空白文本定位唯一片段，保留其余真实 JavaScript 实现。"""
    pattern = r"\s+".join(re.escape(part) for part in original.split())
    updated, count = re.subn(pattern, lambda _match: replacement, source)
    assert count == 1, f"Expected one JavaScript source match, found {count}: {original}"
    return updated


def has_js_source(source: str, fragment: str) -> bool:
    """只忽略格式空白，继续校验声明、常量和表达式本身。"""
    pattern = r"\s+".join(re.escape(part) for part in fragment.split())
    return re.search(pattern, source) is not None
