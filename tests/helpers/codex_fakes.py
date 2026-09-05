"""Codex runner 测试使用的内存流式子进程替身。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

StreamExchange = Callable[[bytes], Awaitable[tuple[bytes, bytes]]]


class _MemoryStdin:
    def __init__(self, process: CallbackStreamingProcess) -> None:
        self._process = process
        self._closed  = False

    def write(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("stdin is closed")
        self._process.stdin_payload.extend(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True
        self._process._stdin_closed.set()

    async def wait_closed(self) -> None:
        return None


class _MemoryReader:
    def __init__(self, process: CallbackStreamingProcess, index: int) -> None:
        self._process = process
        self._index   = index
        self._offset  = 0

    async def read(self, size: int) -> bytes:
        output = (await self._process._exchange_result())[self._index]
        if self._offset >= len(output):
            return b""
        end          = min(len(output), self._offset + max(1, size))
        chunk        = output[self._offset : end]
        self._offset = end
        return chunk


class CallbackStreamingProcess:
    """按真实 asyncio 子进程管道契约执行一次内存交换。"""

    def __init__(
        self,
        exchange: StreamExchange,
        *,
        pid: int               = 43_210,
        returncode: int | None = None,
    ) -> None:
        self.pid                                                      = pid
        self.returncode                                               = returncode
        self.stdin_payload                                            = bytearray()
        self._exchange                                                = exchange
        self._stdin_closed                                            = asyncio.Event()
        self._exchange_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        self.stdin                                                    = _MemoryStdin(self)
        self.stdout                                                   = _MemoryReader(self, 0)
        self.stderr                                                   = _MemoryReader(self, 1)

    async def _exchange_result(self) -> tuple[bytes, bytes]:
        await self._stdin_closed.wait()
        if self._exchange_task is None:
            self._exchange_task = asyncio.create_task(self._exchange(bytes(self.stdin_payload)))
        return await asyncio.shield(self._exchange_task)

    async def wait(self) -> int:
        await self._exchange_result()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self._stdin_closed.set()
