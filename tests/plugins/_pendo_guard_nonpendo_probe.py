# 隔离子进程探针：验证非 Pendo 测试同样受到数据库资源保护。
"""Explicit subprocess probe; the filename intentionally avoids test discovery."""

import sys


def test_non_pendo_plugin_test_does_not_import_pendo_database() -> None:
    assert "plugins.pendo.services.db" not in sys.modules
