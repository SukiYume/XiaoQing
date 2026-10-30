"""插件执行外部命令的统一方式：进程树归属、输出预算、超时与取消时整树回收。

``/shell`` 和小青 agent 的本机工具都用这一套。Windows 上命令先由 ``windows_job.py``
启动门控进程放进 Job 对象，关闭 Job 时整棵进程树一起结束；POSIX 上命令拥有独立的
进程组，结束时整组回收。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CAPTURE_BYTES     = 2 * 1024 * 1024
_PROCESS_STOP_TIMEOUT = 5.0
_EXIT_POLL_SECONDS    = 0.05
_WINDOWS_JOB_SCRIPT   = Path(__file__).with_name("windows_job.py")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """命令的结局；超时或输出超限被终止时 ``exit_code`` 为 ``None``，已读到的输出照样保留。"""

    exit_code     : int | None
    stdout        : str
    stderr        : str
    timed_out     : bool = False
    output_limited: bool = False


def smart_decode(data: bytes) -> str:
    """优先 UTF-8；Windows 本地程序的输出再回退到 GBK。"""

    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 输出预算可能截断在一个多字节字符中间，不能因此整段改用 GBK 解读。
        if exc.reason == "unexpected end of data" and exc.end == len(data):
            return data[: exc.start].decode("utf-8", errors="replace")
    if sys.platform == "win32":
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def _subprocess_group_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": creationflags} if creationflags else {}
    return {"start_new_session": True}


async def _wait_for_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (TimeoutError, OSError):
        return False
    return True


async def _kill_directly(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(OSError, AttributeError):
            proc.kill()
    await _wait_for_exit(proc, _PROCESS_STOP_TIMEOUT)


async def _terminate_posix_tree(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        await _kill_directly(proc)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        await _kill_directly(proc)
        return
    if not await _wait_for_exit(proc, _PROCESS_STOP_TIMEOUT):
        await _kill_directly(proc)


def _sweep_posix_group(pid: int) -> None:
    """命令退出后清掉它留在同一进程组里的后台子进程；Windows 由 Job 对象负责。

    后台子进程可能早已关闭或重定向了输出管道，管道收尾看不出它还在运行。
    """

    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)


async def _spawn(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> tuple[asyncio.subprocess.Process, Any]:
    """启动命令；Windows 先把启动门控进程放进 Job，再放行真正的命令树。"""

    windows_job = None
    launch      = list(argv)
    if sys.platform == "win32":
        launch = [sys.executable, str(_WINDOWS_JOB_SCRIPT), *argv]
    proc = await asyncio.create_subprocess_exec(
        *launch,
        cwd    = str(cwd) if cwd is not None else None,
        env    = dict(env) if env is not None else None,
        stdin  = asyncio.subprocess.PIPE if sys.platform == "win32" else asyncio.subprocess.DEVNULL,
        stdout = asyncio.subprocess.PIPE,
        stderr = asyncio.subprocess.PIPE,
        **_subprocess_group_kwargs(),
    )
    if sys.platform == "win32":
        from .windows_job import WindowsJob

        try:
            windows_job = WindowsJob(proc.pid)
            assert proc.stdin is not None
            proc.stdin.write(b"1")
            await proc.stdin.drain()
            proc.stdin.close()
        except BaseException:
            if windows_job is not None:
                windows_job.close()
            await _kill_directly(proc)
            raise
    return proc, windows_job


async def run_process(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None              = None,
    env: Mapping[str, str] | None = None,
    max_capture_bytes: int        = MAX_CAPTURE_BYTES,
) -> ProcessResult:
    """执行命令直到退出、超时、输出超限或被取消；任何结局都回收整个进程树。

    stdout 与 stderr 共用 ``max_capture_bytes`` 的预算。命令自己退出后，留下的后台子进程
    若还占着输出管道，最多再等到截止时间（且不超过 5 秒）就整树回收，不会因此额外得到
    执行时间。
    """

    if not argv:
        raise ValueError("command arguments must not be empty")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("command timeout must be finite and positive")
    loop     = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    proc, windows_job = await _spawn(argv, cwd=cwd, env=env)
    overflow                        = asyncio.Event()
    captured                        = 0
    buffers: dict[str, list[bytes]] = {"stdout": [], "stderr": []}

    async def terminate() -> None:
        if windows_job is not None:
            windows_job.close()
            if not await _wait_for_exit(proc, _PROCESS_STOP_TIMEOUT):
                await _kill_directly(proc)
        else:
            await _terminate_posix_tree(proc)

    async def read_limited(stream: asyncio.StreamReader | None, name: str) -> None:
        # 超出预算后继续读到 EOF 并丢弃：停止读取会让管道传输暂停，进程树被杀后也无法关闭。
        nonlocal captured
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            remaining = max_capture_bytes - captured
            if remaining > 0:
                buffers[name].append(chunk[:remaining])
                captured += min(len(chunk), remaining)
            # 只有真的读到超出预算的字节才算超限；恰好等于预算的输出正常结束。
            if len(chunk) > remaining:
                overflow.set()

    async def wait_exit() -> None:
        # ``Process.wait()`` 还要等输出管道关闭；留下后台子进程的命令会一直卡到超时，
        # 所以直接看退出码，退出后再给管道一个有限的收尾窗口。
        while proc.returncode is None:
            await asyncio.sleep(_EXIT_POLL_SECONDS)

    stdout_task   = asyncio.create_task(read_limited(proc.stdout, "stdout"))
    stderr_task   = asyncio.create_task(read_limited(proc.stderr, "stderr"))
    wait_task     = asyncio.create_task(wait_exit())
    overflow_task = asyncio.create_task(overflow.wait())
    timed_out     = False
    try:
        done, _pending = await asyncio.wait(
            {wait_task, overflow_task},
            timeout     = timeout,
            return_when = asyncio.FIRST_COMPLETED,
        )
        terminated = overflow_task in done or wait_task not in done
        if terminated:
            timed_out = overflow_task not in done
            await terminate()
        # 已经终止的进程树给独立的收尾时间；正常退出的命令不能借收尾多跑。
        drain = (
            _PROCESS_STOP_TIMEOUT
            if terminated
            else min(_PROCESS_STOP_TIMEOUT, max(0.0, deadline - loop.time()))
        )
        _drained, lingering = await asyncio.wait({stdout_task, stderr_task}, timeout=drain)
        if lingering:
            # 命令已结束但后台子进程仍占着输出管道：回收整个树，保留已读到的输出。
            timed_out = timed_out or loop.time() >= deadline
            await terminate()
        limited = overflow.is_set()
        return ProcessResult(
            exit_code      = None if timed_out or limited else proc.returncode,
            stdout         = smart_decode(b"".join(buffers["stdout"])),
            stderr         = smart_decode(b"".join(buffers["stderr"])),
            timed_out      = timed_out,
            output_limited = limited,
        )
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(terminate())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        raise
    finally:
        if windows_job is not None:
            windows_job.close()
        if proc.returncode is None:
            await terminate()
        _sweep_posix_group(proc.pid)
        # 进程树已经结束，读取方很快会读到 EOF；让它们读完，管道传输才能正常关闭。
        await asyncio.wait({stdout_task, stderr_task}, timeout=_PROCESS_STOP_TIMEOUT)
        for task in (stdout_task, stderr_task, wait_task, overflow_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            stdout_task,
            stderr_task,
            wait_task,
            overflow_task,
            return_exceptions=True,
        )
