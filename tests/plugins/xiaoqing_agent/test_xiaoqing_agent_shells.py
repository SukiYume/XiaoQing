"""agent 跑命令用的 shell：按配置或平台默认选择，选出来的 shell 能直接执行命令文本。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.process_tree import run_process
from plugins.xiaoqing_agent import shells
from plugins.xiaoqing_agent.shells import resolve_shell
from tests.helpers.asyncio_tools import run_with_subprocess_loop


@pytest.mark.parametrize(
    ("configured", "label", "tail"),
    [
        ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "PowerShell", "-Command"),
        ("pwsh", "PowerShell", "-Command"),
        ("C:/Windows/System32/cmd.exe", "cmd.exe", "/c"),
        ("/usr/bin/zsh", "zsh", "-c"),
    ],
)
def test_configured_shell_uses_the_matching_command_flag(
    configured: str,
    label: str,
    tail: str,
) -> None:
    spec = resolve_shell(configured)
    assert label in spec.label
    assert spec.argv("echo hi")[-2:] == [tail, "echo hi"]
    assert spec.argv("echo hi")[0] == configured


def test_auto_shell_is_runnable_on_this_host() -> None:
    spec = resolve_shell("")
    assert spec.argv_prefix
    if sys.platform == "win32":
        assert "windowsapps" not in spec.argv_prefix[0].lower()


@pytest.mark.skipif(sys.platform != "win32", reason="WSL 启动器只存在于 Windows")
def test_windows_auto_shell_skips_the_wsl_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str | None:
        if name == "bash":
            return r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.exe"
        if name == "powershell":
            return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        return None

    monkeypatch.setattr(shells.shutil, "which", which)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    spec = resolve_shell("")
    assert spec.label == "PowerShell"


def test_auto_shell_executes_a_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        spec = resolve_shell("")
        result = await run_process(spec.argv("echo agent-ok"), cwd=tmp_path, timeout=60)
        assert result.exit_code == 0
        assert "agent-ok" in result.stdout

    run_with_subprocess_loop(scenario)
