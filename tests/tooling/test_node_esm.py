"""测试客户端执行器对缺失 Node.js 的错误处理。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import node_esm


def test_node_requirement_fails_when_runtime_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XIAOQING_REQUIRE_NODE", "1")
    monkeypatch.setattr(node_esm.shutil, "which", lambda _name: None)

    with pytest.raises(pytest.fail.Exception, match="Node.js is required"):
        node_esm.assert_node_esm_contract("export {};", "", cwd=tmp_path)
