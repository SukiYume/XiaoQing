"""跨插件共享的测试 fixture。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_data_dir():
    """提供短路径的独立数据目录，避免超长参数化名称进入 Windows 路径。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)
