"""Bot monitor 与日志轮转测试共享的进程探针。"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
MONITOR = ROOT / "scripts" / "run-bot-monitor.ps1"
STOP_LAUNCHER = ROOT / "scripts" / "stop-bot.vbs"
LOG_PUMP = ROOT / "scripts" / "run_process_with_rotating_logs.py"
POWERSHELL_TIMEOUT_SECONDS = 30


def pid_marker_child_source() -> str:
    """生成先原子发布 PID、再保持运行的子进程探针。"""

    return (
        "import os, pathlib, sys, time; "
        "marker = pathlib.Path(sys.argv[1]); "
        "pending = marker.with_name(marker.name + '.tmp'); "
        "pending.write_text(str(os.getpid()), encoding='utf-8'); "
        "os.replace(pending, marker); "
        "time.sleep(60)"
    )


def powershell_executable() -> str | None:
    """返回当前环境可执行的 PowerShell 宿主。"""

    return shutil.which("powershell.exe") or shutil.which("pwsh")


def powershell_output_encoding(executable: str) -> str:
    """返回 PowerShell 宿主实际使用的管道文本编码。"""

    if os.name == "nt" and Path(executable).name.casefold() == "powershell.exe":
        # Windows PowerShell 5.1 按系统代码页写入重定向管道；显式读取本机
        # 编码可以避免 Python UTF-8 模式改变诊断文本。
        return locale.getencoding()
    return "utf-8"


def run_powershell(
    executable: str,
    *arguments: str,
    env: dict[str, str] | None = None,
    timeout: float = POWERSHELL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """运行 PowerShell，并稳定解码不同宿主产生的诊断输出。"""

    return subprocess.run(
        [executable, "-NoLogo", "-NoProfile", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding=powershell_output_encoding(executable),
        errors="replace",
        env=env,
        timeout=timeout,
    )


def assert_process_not_running(process_id: int) -> None:
    """跨平台确认指定 PID 已经退出。"""

    if os.name == "nt":
        executable = powershell_executable()
        assert executable is not None
        environment = {**os.environ, "XIAOQING_TEST_CHILD_PID": str(process_id)}
        result = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-Command",
                "if (Get-Process -Id $env:XIAOQING_TEST_CHILD_PID "
                "-ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }",
            ],
            check=False,
            capture_output=True,
            env=environment,
            timeout=POWERSHELL_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0
        return

    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
