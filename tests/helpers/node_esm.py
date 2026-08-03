"""在 Node.js 中执行浏览器 ESM 源码契约的共享测试辅助。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def assert_node_esm_contract(
    module_source: str,
    script: str,
    *,
    cwd: Path,
    setup: str = "",
) -> None:
    """把源码作为独立 ESM 导入，并断言随后的 JavaScript 契约成功执行。"""

    node = shutil.which("node")
    if node is None:
        if os.environ.get("XIAOQING_REQUIRE_NODE") == "1":
            pytest.fail("Node.js is required for browser ESM contract tests")
        pytest.skip("Node.js is not installed")
    assert node is not None

    module_url = "data:text/javascript;base64," + base64.b64encode(
        module_source.encode("utf-8")
    ).decode("ascii")
    source = f"""
        import assert from 'node:assert/strict';
        {setup}
        const client = await import({json.dumps(module_url)});
        {script}
    """
    result = subprocess.run(
        [node, "--input-type=module"],
        cwd=cwd,
        input=source,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
