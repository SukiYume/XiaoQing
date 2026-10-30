"""agent 跑命令用哪个 shell：按配置，或按平台默认挑一个能直接执行命令文本的。

命令本身的执行、进程树回收和输出预算在 ``core.process_tree``。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# WindowsApps 下的 bash.exe 是 WSL 启动器，不是可直接执行命令的 bash。
_WSL_LAUNCHER_DIRS = ("\\windowsapps\\", "\\system32\\")


@dataclass(frozen=True, slots=True)
class ShellSpec:
    """一个可执行 shell 及其“执行一段命令文本”的参数写法。"""

    label      : str
    argv_prefix: tuple[str, ...]

    def argv(self, command: str) -> list[str]:
        return [*self.argv_prefix, command]


def _shell_spec_for(path: str) -> ShellSpec:
    name = Path(path).name.lower()
    if name.startswith(("powershell", "pwsh")):
        return ShellSpec("PowerShell", (path, "-NoProfile", "-NonInteractive", "-Command"))
    if name in {"cmd", "cmd.exe"}:
        return ShellSpec("cmd.exe", (path, "/d", "/s", "/c"))
    label = "Git Bash" if sys.platform == "win32" else Path(path).name
    return ShellSpec(f"{label} ({path})", (path, "-c"))


def _git_bash_candidates() -> list[str]:
    candidates: list[str] = []
    git                   = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        candidates += [
            str(git_root / "bin" / "bash.exe"),
            str(git_root.parent / "bin" / "bash.exe"),
        ]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base:
            candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    found = shutil.which("bash")
    if found and not any(part in found.lower() for part in _WSL_LAUNCHER_DIRS):
        candidates.append(found)
    return candidates


def resolve_shell(configured: str = "") -> ShellSpec:
    """按配置或平台默认选出命令 shell；Windows 优先 Git Bash，没有时退回 PowerShell。"""

    if configured.strip():
        return _shell_spec_for(configured.strip())
    if sys.platform == "win32":
        for candidate in _git_bash_candidates():
            if Path(candidate).is_file():
                return _shell_spec_for(candidate)
        powershell = shutil.which("powershell") or "powershell.exe"
        return _shell_spec_for(powershell)
    for candidate in ("/bin/bash", "/usr/bin/bash", "/bin/sh"):
        if Path(candidate).is_file():
            return _shell_spec_for(candidate)
    return _shell_spec_for("sh")
