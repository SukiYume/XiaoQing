"""
arxiv_filter 插件公共工具
"""

import json
from pathlib import Path
from typing import Any


def load_plugin_config() -> dict[str, Any]:
    """加载插件 config.json，文件不存在时返回空字典"""
    config_path = Path(__file__).with_name("config.json")
    if not config_path.exists():
        return {}
    with config_path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("arxiv_filter config.json must contain a JSON object")
    return payload
