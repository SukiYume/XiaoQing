"""统一的子进程执行：退出码、超时、输出上限、取消和进程树回收都用真实进程验证。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from core.process_tree import run_process, smart_decode
from tests.helpers.asyncio_tools import run_with_subprocess_loop

PY = sys.executable


def _grandchild_script(marker: Path, delay: float) -> str:
    """父进程启动一个孙进程：孙进程睡 ``delay`` 秒后写标记文件。"""

    child = (
        f"import time, pathlib; time.sleep({delay}); pathlib.Path({str(marker)!r}).write_text('x')"
    )
    return (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(60)\n"
    )


def test_run_process_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    async def scenario() -> None:
        result = await run_process(
            [PY, "-c", "import sys; print('你好'); print('坏了', file=sys.stderr); sys.exit(3)"],
            cwd     = tmp_path,
            timeout = 30,
            env     = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert result.exit_code == 3
        assert result.stdout.strip() == "你好"
        assert result.stderr.strip() == "坏了"
        assert result.timed_out is False and result.output_limited is False

    run_with_subprocess_loop(scenario)


def test_run_process_uses_the_requested_working_directory(tmp_path: Path) -> None:
    async def scenario() -> None:
        result = await run_process(
            [PY, "-c", "import os; print(os.getcwd())"], cwd=tmp_path, timeout=30
        )
        assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()

    run_with_subprocess_loop(scenario)


@pytest.mark.slow
def test_timeout_terminates_the_whole_process_tree(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker  = tmp_path / "grandchild-survived"
        started = time.monotonic()
        result  = await run_process(
            [PY, "-c", _grandchild_script(marker, 3)], cwd=tmp_path, timeout=1
        )
        assert result.timed_out is True
        assert result.exit_code is None
        assert time.monotonic() - started < 15
        await asyncio.sleep(4)
        assert not marker.exists(), "超时后孙进程仍在运行"

    run_with_subprocess_loop(scenario)


@pytest.mark.slow
def test_cancellation_terminates_the_whole_process_tree(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker  = tmp_path / "grandchild-survived"
        running = asyncio.create_task(
            run_process([PY, "-c", _grandchild_script(marker, 3)], cwd=tmp_path, timeout=60)
        )
        await asyncio.sleep(1.0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(4)
        assert not marker.exists(), "取消后孙进程仍在运行"

    run_with_subprocess_loop(scenario)


def test_output_budget_stops_runaway_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        result = await run_process(
            [PY, "-c", "import sys\nwhile True: sys.stdout.write('x' * 4096)"],
            cwd               = tmp_path,
            timeout           = 30,
            max_capture_bytes = 64 * 1024,
        )
        assert result.output_limited is True
        assert result.exit_code is None
        assert len(result.stdout) <= 64 * 1024

    run_with_subprocess_loop(scenario)


@pytest.mark.slow
def test_background_child_holding_pipes_does_not_hang_the_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        script = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print('parent done', flush=True)\n"
        )
        started = time.monotonic()
        result = await run_process([PY, "-c", script], cwd=tmp_path, timeout=30)
        assert time.monotonic() - started < 20
        assert "parent done" in result.stdout
        assert result.timed_out is False

    run_with_subprocess_loop(scenario)


def test_background_child_with_closed_output_is_reaped_after_normal_exit(tmp_path: Path) -> None:
    marker = tmp_path / "survived.txt"
    child  = f"import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('x')"
    script = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.DEVNULL,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('parent done', flush=True)\n"
    )

    async def scenario() -> None:
        result = await run_process([PY, "-c", script], cwd=tmp_path, timeout=30)
        assert result.exit_code == 0 and "parent done" in result.stdout
        await asyncio.sleep(4)
        assert not marker.exists()

    run_with_subprocess_loop(scenario)


@pytest.mark.asyncio
async def test_run_process_rejects_invalid_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await run_process([], cwd=tmp_path, timeout=1)
    with pytest.raises(ValueError):
        await run_process([PY, "-c", "pass"], cwd=tmp_path, timeout=0)


def test_smart_decode_keeps_utf8_when_budget_cuts_a_character() -> None:
    data = "温度 25℃".encode()
    assert smart_decode(data[:-1]) == "温度 25\ufffd" or smart_decode(data[:-1]).startswith(
        "温度 25"
    )
    assert smart_decode(b"") == ""
    assert smart_decode("中文".encode()) == "中文"


@pytest.mark.skipif(sys.platform != "win32", reason="GBK 回退只用于 Windows 本地程序输出")
def test_smart_decode_falls_back_to_gbk_on_windows() -> None:
    assert smart_decode("驱动器 C 中的卷".encode("gbk")) == "驱动器 C 中的卷"
    assert smart_decode("中".encode("gbk")) == "中"


@pytest.mark.slow
def test_repeated_cancellation_still_reaps_the_tree(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker  = tmp_path / "grandchild-survived"
        running = asyncio.create_task(
            run_process([PY, "-c", _grandchild_script(marker, 3)], cwd=tmp_path, timeout=60)
        )
        await asyncio.sleep(1.0)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.sleep(4)
        assert not marker.exists(), "两次取消后孙进程仍在运行"

    run_with_subprocess_loop(scenario)


def test_a_child_holding_pipes_gets_no_time_past_the_deadline(tmp_path: Path) -> None:
    # 父进程立刻退出、留下的子进程占着输出管道：收尾不能超出命令自己的时限。
    marker = tmp_path / "escaped.txt"
    child  = (
        "import time; from pathlib import Path; time.sleep(1); Path("
        + repr(str(marker))
        + ").write_text('escaped')"
    )
    parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"

    async def scenario() -> None:
        started = time.monotonic()
        result = await run_process([PY, "-c", parent], cwd=tmp_path, timeout=0.2)
        assert result.timed_out is True and result.exit_code is None
        assert time.monotonic() - started < 0.9
        await asyncio.sleep(1.05)
        assert not marker.exists()

    run_with_subprocess_loop(scenario)


def test_smart_decode_replaces_unknown_bytes() -> None:
    assert smart_decode(b"\xff") == "\ufffd"
    assert smart_decode(b"prefix\xff") == "prefix\ufffd"
