"""命令的发送回执：agent 调用的命令（比如取图）带着“发没发出去”的回执，随附件的实际发送结算。

命令原本要发的那条消息不会原样发出，模型拿到的是文字和附件路径。附件真正随某次发送发出时，
按那次发送的结果提交、回滚或记为未知；任务结束时还没发出的一律回滚，释放命令为这次发送做的
预留（比如取图占用的图片、日报占用的当日名额），否则之后同样的请求会被这份预留挡住。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from core.delivery import DeliveryReceipt


@dataclass(frozen=True)
class HeldReceipt:
    """一次命令调用的回执，以及这次调用带回、等着发出去的附件。"""

    receipt: DeliveryReceipt
    files: frozenset[str]


def file_key(path: str | Path) -> str:
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path)
    return os.path.normcase(str(resolved))


async def hold(
    held: list[HeldReceipt],
    receipt: DeliveryReceipt,
    files: Iterable[str | Path],
) -> None:
    """有附件就等附件发出时结算；只有文字时原消息不会发出，当场回滚。"""

    keys = frozenset(file_key(path) for path in files)
    if keys:
        held.append(HeldReceipt(receipt, keys))
    else:
        await asyncio.shield(receipt.record(False))


async def settle(
    held: list[HeldReceipt],
    files: Iterable[str | Path],
    delivered: bool | None,
) -> None:
    """一次发送带出了这些附件：含有其中任何一个的回执按这次发送的结果结算。"""

    keys    = {file_key(path) for path in files}
    matched = [item for item in held if item.files & keys]
    for item in matched:
        held.remove(item)
    for item in matched:
        await asyncio.shield(item.receipt.record(delivered))


async def release(held: list[HeldReceipt], *, keep: Iterable[str | Path] = ()) -> None:
    """没发出去的回执一律回滚；``keep`` 里的附件还在等发送结果，先不动。"""

    kept     = {file_key(path) for path in keep}
    released = [item for item in held if not item.files & kept]
    for item in released:
        held.remove(item)
    for item in released:
        await asyncio.shield(item.receipt.record(False))
