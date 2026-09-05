"""Windows 命令树的 Job 所有权与启动握手。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class WindowsJob:
    """关闭句柄时收敛整个命令树，父命令提前退出后仍保留归属。"""

    def __init__(self, pid: int) -> None:
        # 平台边界同时约束运行调用与静态检查，Windows API 仅在对应平台可用。
        if sys.platform != "win32":
            raise OSError("Windows Job objects require Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel                            = kernel
        kernel.CreateJobObjectW.argtypes        = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype         = wintypes.HANDLE
        kernel.OpenProcess.argtypes             = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype              = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes              = [wintypes.HANDLE]
        self._handle                             = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits             = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not kernel.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self) -> None:
        if sys.platform != "win32":
            return
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


if __name__ == "__main__":
    # 管理者完成 Job 归属后才放行，消除启动与登记之间的子进程逃逸窗口。
    if sys.stdin.buffer.read(1) != b"1":
        raise SystemExit(1)
    raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
