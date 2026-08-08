"""测试套件使用的稳定仓库路径。

测试文件会按职责移动到不同层级；所有需要读取仓库资源的测试都从这里取得
根目录，避免目录重排改变 ``Path(__file__)`` 的含义。
"""

from __future__ import annotations

from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TESTS_ROOT.parent
PLUGINS_ROOT = REPOSITORY_ROOT / "plugins"
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
