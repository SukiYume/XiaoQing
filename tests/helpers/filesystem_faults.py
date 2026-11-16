"""文件系统故障注入：在操作系统边界制造写盘失败，其余路径走生产代码。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def fail_replace_onto(monkeypatch: pytest.MonkeyPatch, filename: str, message: str) -> None:
    """让原子替换的目标文件名等于 ``filename`` 时抛出 ``OSError(message)``，其他替换照常执行。

    原子写入先写临时文件再 ``os.replace`` 到目标；在这一步失败，等于磁盘在落盘的最后一刻写满。
    """

    real_replace = os.replace

    def failing_replace(source: object, destination: object) -> None:
        if Path(str(destination)).name == filename:
            raise OSError(message)
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", failing_replace)
