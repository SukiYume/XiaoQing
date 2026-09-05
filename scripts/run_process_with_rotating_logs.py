"""运行一个子进程，并把 stdout/stderr 分别写入有界轮转日志。

监控器不让 ``Start-Process`` 直接重定向活动日志：Windows 的继承句柄会阻止监控器
重命名正在写入的文件。本模块为两条流各分配一个泵线程，由文件唯一所有者在轮转前
显式关闭句柄，轮转后立即重开。

每条输出流只由一个泵线程读取并写入自己的日志，因此轮转不需要跨线程文件锁。日志
写入一旦失败，泵线程仍要继续排空管道并通知主线程终止整棵子进程树，避免子进程因
管道写满而卡死；返回前必须回收进程、管道和两个泵线程，不能遗留后台写入者。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

_READ_SIZE                = 64 * 1024
_COPY_SIZE                = 1024 * 1024
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


# 日志文件所有权与轮转 ---------------------------------------------------------


class BoundedRotatingLog:
    """按字节追加日志，并严格限制单文件大小和备份数量。"""

    def __init__(self, path: Path, *, maximum_bytes: int, backup_count: int) -> None:
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        if backup_count <= 0:
            raise ValueError("backup_count must be positive")

        self.path                     = path
        self.maximum_bytes            = maximum_bytes
        self.backup_count             = backup_count
        self._stream: BinaryIO | None = None
        self._size                    = 0

    def __enter__(self) -> BoundedRotatingLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_excess_backups()
        for index in range(1, self.backup_count + 1):
            self._trim_to_limit(self._backup_path(index))
        self._trim_to_limit(self.path)
        if self.path.exists() and self.path.stat().st_size >= self.maximum_bytes:
            self._rotate()
        else:
            self._open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def write(self, data: bytes) -> None:
        if self._stream is None:
            raise RuntimeError("log is not open")

        view   = memoryview(data)
        offset = 0
        while offset < len(view):
            if self._size >= self.maximum_bytes:
                self._rotate()
            writable = min(self.maximum_bytes - self._size, len(view) - offset)
            written  = self._stream.write(view[offset : offset + writable])
            if written is None or written <= 0:
                raise OSError("log write made no progress")
            self._size += written
            offset += written

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _open(self) -> None:
        self._stream = self.path.open("ab", buffering=0)
        self._size = self.path.stat().st_size

    def _rotate(self) -> None:
        self.close()
        try:
            oldest = self._backup_path(self.backup_count)
            oldest.unlink(missing_ok=True)
            for index in range(self.backup_count - 1, 0, -1):
                source = self._backup_path(index)
                if source.exists():
                    os.replace(source, self._backup_path(index + 1))
            if self.path.exists() and self.path.stat().st_size:
                os.replace(self.path, self._backup_path(1))
            else:
                self.path.unlink(missing_ok=True)
        finally:
            self._open()

    def _remove_excess_backups(self) -> None:
        prefix = f"{self.path.name}."
        for candidate in self.path.parent.glob(f"{self.path.name}.*"):
            suffix = candidate.name[len(prefix) :]
            if suffix.isdecimal():
                index = int(suffix)
                if suffix != str(index) or not 1 <= index <= self.backup_count:
                    candidate.unlink(missing_ok=True)

    def _trim_to_limit(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size <= self.maximum_bytes:
            return

        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.trim")
        try:
            with path.open("rb") as source:
                source.seek(-self.maximum_bytes, os.SEEK_END)
                with temporary.open("xb") as target:
                    remaining = self.maximum_bytes
                    while remaining:
                        chunk = source.read(min(_COPY_SIZE, remaining))
                        if not chunk:
                            break
                        target.write(chunk)
                        remaining -= len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


# 管道泵线程 -------------------------------------------------------------------


def _pump_stream(
    stream: BinaryIO,
    *,
    log_path: Path,
    maximum_bytes: int,
    backup_count: int,
    errors: list[Exception],
    failed: threading.Event,
    stopping: threading.Event,
) -> None:
    """日志失败立即通知主线程，但继续排空管道直到子进程停止。"""

    log: BoundedRotatingLog | None = None
    logging_failed                 = False
    try:
        try:
            log = BoundedRotatingLog(
                log_path,
                maximum_bytes = maximum_bytes,
                backup_count  = backup_count,
            )
            log.__enter__()
        # 日志对象可能来自测试替身或平台文件层；任意失败都必须转成停机信号，
        # 但泵线程不能退出，否则子进程可能先阻塞在写满的管道上。
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            failed.set()
            logging_failed = True

        try:
            while chunk := stream.read(_READ_SIZE):
                # 日志已经失败时仍丢弃式排空管道，直到主线程完成进程树终止；
                # 直接退出会让尚未收到终止信号的子进程阻塞在 stdout/stderr。
                if logging_failed or log is None:
                    continue
                try:
                    log.write(chunk)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    failed.set()
                    logging_failed = True
        # 二进制管道实现的异常类型并不统一，资源边界必须报告后继续收口。
        except Exception as exc:  # noqa: BLE001
            if not stopping.is_set():
                errors.append(exc)
                failed.set()
    finally:
        if log is not None:
            try:
                log.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                failed.set()
        try:
            stream.close()
        except Exception as exc:  # noqa: BLE001
            if not stopping.is_set():
                errors.append(exc)
                failed.set()


# 跨平台进程树回收 -------------------------------------------------------------


def _send_posix_group_signal(process_id: int, signal_number: int) -> bool:
    """向独立 POSIX 进程组发信号；组已消失时返回 ``False``。"""

    kill_process_group = getattr(os, "killpg", None)
    if not callable(kill_process_group):
        return False
    try:
        kill_process_group(process_id, signal_number)
    except ProcessLookupError:
        return False
    return True


def _wait_for_posix_group_exit(process_id: int, timeout: float) -> bool:
    """用信号 0 有界等待 POSIX 进程组全部消失。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _send_posix_group_signal(process_id, 0):
            return True
        time.sleep(0.05)
    return not _send_posix_group_signal(process_id, 0)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
) -> tuple[int, Exception | None]:
    """停止本包装器创建的进程树，并返回主进程退出码与清理异常。"""

    tree_error: Exception | None = None
    group_signaled               = False
    if os.name == "nt":
        if process.poll() is not None:
            return process.returncode, None
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        taskkill    = system_root / "System32" / "taskkill.exe"
        try:
            completed = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin         = subprocess.DEVNULL,
                stdout        = subprocess.DEVNULL,
                stderr        = subprocess.DEVNULL,
                check         = False,
                timeout       = 5,
                creationflags = subprocess.CREATE_NO_WINDOW,
            )
            if completed.returncode != 0:
                tree_error = RuntimeError(
                    f"taskkill failed for process tree {process.pid} "
                    f"with exit code {completed.returncode}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            tree_error = RuntimeError(f"taskkill failed for process tree {process.pid}: {exc}")
    else:
        try:
            group_signaled = _send_posix_group_signal(process.pid, signal.SIGTERM)
        except OSError as exc:
            group_signaled = False
            tree_error = RuntimeError(f"process group termination failed for {process.pid}: {exc}")
        if not group_signaled and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

    if process.poll() is None and tree_error is not None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        return_code = process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timeout_error = RuntimeError(f"process {process.pid} did not stop before forced kill")
        if os.name != "nt":
            sigkill = getattr(signal, "SIGKILL", None)
            if sigkill is not None:
                with contextlib.suppress(OSError):
                    _send_posix_group_signal(process.pid, sigkill)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            return (
                process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS),
                tree_error or timeout_error,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"process {process.pid} could not be reaped after kill") from exc

    if os.name != "nt" and group_signaled:
        try:
            group_exited = _wait_for_posix_group_exit(
                process.pid,
                _SHUTDOWN_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            return return_code, tree_error or RuntimeError(
                f"process group exit check failed for {process.pid}: {exc}"
            )
        if not group_exited:
            timeout_error = RuntimeError(f"process group {process.pid} did not stop before kill")
            sigkill       = getattr(signal, "SIGKILL", None)
            if sigkill is not None:
                with contextlib.suppress(OSError):
                    _send_posix_group_signal(process.pid, sigkill)
            tree_error = tree_error or timeout_error
    return return_code, tree_error


def _join_pump_threads(
    threads: Sequence[threading.Thread],
    process: subprocess.Popen[bytes],
    errors: list[Exception],
) -> None:
    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    alive = [thread for thread in threads if thread.is_alive()]
    if not alive:
        return

    try:
        _, cleanup_error = _terminate_process_tree(process)
        if cleanup_error is not None:
            errors.append(cleanup_error)
    # 清理阶段必须聚合所有资源错误，继续关闭管道并尝试回收泵线程。
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    _close_process_pipes(process, errors)
    for thread in alive:
        thread.join(1.0)
    still_alive = [thread.name for thread in alive if thread.is_alive()]
    if still_alive:
        errors.append(RuntimeError(f"log pump threads did not stop: {', '.join(still_alive)}"))


def _close_process_pipes(
    process: subprocess.Popen[bytes],
    errors: list[Exception],
) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)


def _cleanup_owned_process(
    process: subprocess.Popen[bytes],
    threads: Sequence[threading.Thread],
    errors: list[Exception],
) -> None:
    try:
        _, cleanup_error = _terminate_process_tree(process)
        if cleanup_error is not None:
            errors.append(cleanup_error)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    _close_process_pipes(process, errors)
    _join_pump_threads(threads, process, errors)


# 子进程编排与命令行 -----------------------------------------------------------


def run_process(
    command: Sequence[str],
    *,
    working_directory: Path,
    stdout_log: Path,
    stderr_log: Path,
    maximum_bytes: int,
    backup_count: int,
) -> int:
    """启动命令并同步排空两条输出流，返回被包装进程的退出码。"""

    if not command:
        raise ValueError("command must not be empty")
    if not working_directory.is_dir():
        raise ValueError("working_directory must be an existing directory")
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    if backup_count <= 0:
        raise ValueError("backup_count must be positive")
    if stdout_log.resolve() == stderr_log.resolve():
        raise ValueError("stdout and stderr logs must be different files")

    creation_flags = 0
    startup_info   = None
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW
        startup_info   = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE

    errors: list[Exception]                 = []
    started_threads: list[threading.Thread] = []
    stopping: threading.Event | None        = None
    cleaned_up                              = False
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            list(command),
            cwd           = working_directory,
            stdin         = subprocess.DEVNULL,
            stdout        = subprocess.PIPE,
            stderr        = subprocess.PIPE,
            bufsize       = 0,
            creationflags = creation_flags,
            startupinfo   = startup_info,
            # POSIX 使用独立会话，清理时才能按进程组终止所有后代；Windows
            # 保持既有 taskkill /T 语义，不改变生产监控链。
            start_new_session=os.name != "nt",
        )
        if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen 契约
            raise RuntimeError("failed to create output pipes")

        failed   = threading.Event()
        stopping = threading.Event()
        common   = {
            "maximum_bytes": maximum_bytes,
            "backup_count": backup_count,
            "errors": errors,
            "failed": failed,
            "stopping": stopping,
        }
        thread_specs = (
            (process.stdout, stdout_log, "stdout-log-pump"),
            (process.stderr, stderr_log, "stderr-log-pump"),
        )
        for stream, log_path, name in thread_specs:
            thread = threading.Thread(
                target = _pump_stream,
                kwargs = {"stream": stream, "log_path": log_path, **common},
                name   = name,
                daemon = True,
            )
            thread.start()
            started_threads.append(thread)

        while True:
            if failed.wait(timeout=0.1):
                # 从这里开始，管道异常属于主动停机的连带结果，不再覆盖最初的日志错误。
                stopping.set()
                return_code, cleanup_error = _terminate_process_tree(process)
                if cleanup_error is not None:
                    errors.append(cleanup_error)
                break
            try:
                return_code = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException as exc:
        if process is None:
            raise
        if stopping is not None:
            stopping.set()
        previous_error_count = len(errors)
        _cleanup_owned_process(process, started_threads, errors)
        cleaned_up = True
        if len(errors) > previous_error_count:
            details = "; ".join(str(error) for error in errors[previous_error_count:])
            raise RuntimeError(f"owned process cleanup failed: {details}") from exc
        raise
    finally:
        if process is not None and not cleaned_up:
            _join_pump_threads(started_threads, process, errors)

    if errors:
        details = "; ".join(str(error) for error in errors)
        raise RuntimeError(f"log pump failed: {details}") from errors[0]
    return return_code


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description     = __doc__,
        formatter_class = argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stdout-log", required=True, type=Path)
    parser.add_argument("--stderr-log", required=True, type=Path)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--backup-count", required=True, type=int)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("a command is required after --")
    if not 64 * 1024 <= arguments.max_bytes <= 10 * 1024**3:
        parser.error("--max-bytes must be between 65536 and 10737418240")
    if not 1 <= arguments.backup_count <= 100:
        parser.error("--backup-count must be between 1 and 100")
    if not arguments.cwd.is_dir():
        parser.error("--cwd must be an existing directory")
    if arguments.stdout_log.resolve() == arguments.stderr_log.resolve():
        parser.error("--stdout-log and --stderr-log must be different files")
    for option, path in (
        ("--stdout-log", arguments.stdout_log),
        ("--stderr-log", arguments.stderr_log),
    ):
        if path.exists() and not path.is_file():
            parser.error(f"{option} must be a file path")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    return run_process(
        arguments.command,
        working_directory = arguments.cwd,
        stdout_log        = arguments.stdout_log,
        stderr_log        = arguments.stderr_log,
        maximum_bytes     = arguments.max_bytes,
        backup_count      = arguments.backup_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
