"""只给本机当前用户看的文件：凭据、登录态这类不能让同机其他账号读到的数据。

插件各自保存的登录凭据（小爱账号、B站 只读登录）用同一套写法：目录收紧到当前用户，文件
以 0600 原子写入。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .atomic_store import atomic_write_bytes


def protect_directory(directory: Path) -> None:
    """收紧目录权限：POSIX 0700；Windows 去掉继承、只给当前用户完全控制。"""

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()  # noqa: S607
        subprocess.run(  # noqa: S603
            ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)F"],  # noqa: S607
            check  = True,
            stdout = subprocess.DEVNULL,
            stderr = subprocess.DEVNULL,
        )
    else:
        directory.chmod(0o700)


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    """把凭据 JSON 原子写到目标文件。

    写入走 ``atomic_write_bytes``：同目录临时文件由 ``mkstemp`` 以 0600 创建，落盘后原子替换，
    替换完的目标文件也是 0600。
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False).encode("utf-8"))
