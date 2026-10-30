"""命令的发送回执：agent 调用的命令（比如取图）带着“发没发出去”的回执，随附件的实际发送结算。

命令原本要发的那条消息不会原样发出，模型拿到的是文字和附件路径。附件真正随某次发送发出时，
按那次发送的结果提交、回滚或记为未知；任务结束时还没发出的一律回滚，释放命令为这次发送做的
预留（比如取图占用的图片、日报占用的当日名额），否则之后同样的请求会被这份预留挡住。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from core.delivery import DeliveryReceipt


@dataclass(eq=False)
class HeldReceipt:
    """一次命令调用的回执，以及这次调用带回、等着发出去的附件。

    一份回执可能关联几个分开发送的附件：每个附件的结果先记在 ``results``，全部附件都有结果后
    才结算整份回执，第一个附件发出时不能替后面的附件作主。
    """

    receipt: DeliveryReceipt
    files  : frozenset[str]
    results: dict[str, bool | None] = field(default_factory=dict)

    def combined(self) -> bool | None:
        """全部确认才算送达，全部失败才算没发；部分送达或任一未知都记为未知。"""

        outcomes = [self.results.get(key, False) for key in self.files]
        if all(item is True for item in outcomes):
            return True
        if all(item is False for item in outcomes):
            return False
        return None


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
    """一次发送带出了这些附件：记下它们的结果，关联附件都有结果的回执随即结算。"""

    keys                        = {file_key(path) for path in files}
    finished: list[HeldReceipt] = []
    for item in held:
        for key in item.files & keys:
            item.results[key] = delivered
        if item.files & keys and item.files <= item.results.keys():
            finished.append(item)
    for item in finished:
        held.remove(item)
    for item in finished:
        await asyncio.shield(item.receipt.record(item.combined()))


async def release(held: list[HeldReceipt], *, keep: Iterable[str | Path] = ()) -> None:
    """不会再发的附件记为没发出，回执按已有结果结算；``keep`` 里的附件还在等发送结果，先不动。

    一个附件都没发出的回执回滚；已经有附件送达或结果未知的，整份回执记为未知。
    """

    kept                        = {file_key(path) for path in keep}
    released: list[HeldReceipt] = []
    for item in held:
        if item.files & kept:
            # 还在等的附件之外，其余附件不会再发：先记为没发出，等候附件有结果时一起结算。
            for key in item.files - kept:
                item.results.setdefault(key, False)
        else:
            released.append(item)
    for item in released:
        held.remove(item)
    for item in released:
        await asyncio.shield(item.receipt.record(item.combined()))
