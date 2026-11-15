"""群友提过的、有时间点的事（考试、面试、出行），时间过了以后合适时顺口关心一句。

记录由投递后的抽取任务写进统一记忆（xiaoqing_memory），按会话保存。一件事只问一次：取用时
给出“顺口问”，发出的回复问到了就记为已问，对方接下来一轮的回复交给模型理解后结束；提示过
几轮仍没问出口的不再提示；对方已经说了结果的，由抽取任务直接结束。

这里是记录的数据结构、同一件事的判断和给回复模型的提示文字。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..utils.wall_clock import format_local


@dataclass(frozen=True)
class OpenLoop:
    loop_id   : str
    user_id   : int
    user_name : str
    title     : str
    due_at    : float
    created_at: float
    status    : str   = "open"
    asked_at  : float = 0.0
    surfaced  : int   = 0


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", str(text or ""))
    return {compact[i : i + 2] for i in range(len(compact) - 1)} or {compact}


def same_topic(left: str, right: str) -> bool:
    a, b = _bigrams(left), _bigrams(right)
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= 0.5


def mentions(reply: str, loop: OpenLoop) -> bool:
    """回复里是否真的提到了这件事（标题里至少两组相邻字出现在回复中）。"""

    compact = re.sub(r"\s+", "", str(reply or ""))
    hits    = sum(1 for pair in _bigrams(loop.title) if len(pair) == 2 and pair in compact)
    return hits >= min(2, len(_bigrams(loop.title)))


def open_loop_block(loop: OpenLoop, mode: str) -> str:
    """给回复模型的一句提示；放在用户消息里，属于低信任参考。"""

    when = format_local("%m月%d日%H:%M", loop.due_at)
    if mode == "review":
        return (
            f"你前面顺口问过对方“{loop.title}”的事，这一轮对方可能是在回答。"
            "接住对方的回答就行，不要再追问同一件事。"
        )
    return (
        f"对方之前提过“{loop.title}”（大约{when}），现在时间已经过了。"
        "只在对方没在说别的急事、话题合适时，像忽然想起来那样顺口问一句结果；"
        "不要说你一直记着，这轮不合适就别提。"
    )
