"""
arxiv_filter 插件公共工具
"""

import json
import os


def load_plugin_config() -> dict:
    """加载插件 config.json，文件不存在时返回空字典"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}
