"""角色自己说出口的近况、打算、承诺和讲过的往事，让她之后说的话前后一致。

记录只来自已经确认发出的角色发言（聊天回复、agent 回复与后台结果、主动开口），由投递后的
抽取任务写进统一记忆（xiaoqing_memory）。近况和打算属于角色自己，只讲她自己时各会话共用；
答应某人的事和涉及对方的近况只在原会话可见。每条都有过期时间，过期或被标记完成后不再取用。

这里是记录的数据结构、校验和给回复模型的提示文字。被问起时讲过的自己以前的事（``story``）
留得久、单独取用：讲过一次就是她的经历，之后再被问到还是同一个版本。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

SelfKind                         = Literal["state", "plan", "promise", "story"]
SELF_KINDS: tuple[SelfKind, ...] = ("state", "plan", "promise", "story")
# 她讲过的自己以前的事：不是近况，不随几小时过期，单独取用。
STORY = "story"
# 每类记录允许的存活小时数（模型给的值会被夹在这个范围内）。
KIND_HOURS: dict[str, tuple[float, float]] = {
    "state"  : (0.5, 12.0),
    "plan"   : (1.0, 48.0),
    "promise": (1.0, 168.0),
    STORY    : (24.0 * 90, 24.0 * 365),
}
_MAX_TEXT_CHARS  = 40
_MAX_STORY_CHARS = 60
_MAX_NAME_CHARS  = 20


def _text_limit(kind: str) -> int:
    return _MAX_STORY_CHARS if kind == STORY else _MAX_TEXT_CHARS


@dataclass(frozen=True)
class SelfEntry:
    entry_id   : str
    ts         : float
    chat_id    : str
    kind       : str
    text       : str
    expires_at : float
    status     : str = "active"
    source     : str = "chat"
    counterpart: str = ""
    # 能否在别的会话里被记得：答应某人的事和私聊里涉及对方的近况只留在原会话。
    shared: bool = True

    def is_active(self, now: float) -> bool:
        return self.status == "active" and self.expires_at > now


def new_entry(
    *,
    chat_id: str,
    kind: str,
    text: str,
    hours: Any,
    source: str,
    counterpart: str  = "",
    now: float | None = None,
    shared: bool      = True,
) -> SelfEntry | None:
    """校验一条候选记录；类别不对或内容为空时返回 None。"""

    clean = " ".join(str(text or "").split())[: _text_limit(kind)]
    if kind not in KIND_HOURS or not clean or not chat_id:
        return None
    low, high = KIND_HOURS[kind]
    try:
        span = float(hours)
    except (TypeError, ValueError):
        span = low
    span    = min(high, max(low, span if span == span else low))
    created = time.time() if now is None else now
    return SelfEntry(
        entry_id    = uuid.uuid4().hex[:8],
        ts          = created,
        chat_id     = chat_id,
        kind        = kind,
        text        = clean,
        expires_at  = created + span * 3600.0,
        source      = str(source or "chat")[:16],
        counterpart = " ".join(str(counterpart or "").split())[:_MAX_NAME_CHARS],
        shared      = bool(shared) and kind != "promise",
    )


def _ago(seconds: float) -> str:
    if seconds < 120:
        return "刚才"
    if seconds < 3600:
        return f"约{int(seconds // 60)}分钟前"
    if seconds < 86400:
        return f"约{int(seconds // 3600)}小时前"
    return f"约{int(seconds // 86400)}天前"


def describe_entry(entry: SelfEntry, now: float) -> str:
    when = _ago(max(0.0, now - entry.ts))
    if entry.kind == "promise":
        who = f"答应{entry.counterpart}" if entry.counterpart else "答应过"
        return f"{when}{who}：{entry.text}（还没办完）"
    if entry.kind == "plan":
        return f"{when}说打算：{entry.text}"
    return f"{when}说：{entry.text}"


def self_timeline_block(
    entries: Sequence[SelfEntry],
    *,
    stories: Sequence[SelfEntry] = (),
    now: float | None            = None,
) -> str:
    """给回复模型的“你最近说过的关于自己的事”，以及她讲过的自己以前的事。"""

    entries           = [entry for entry in entries if isinstance(entry, SelfEntry)]
    told              = [entry for entry in stories if isinstance(entry, SelfEntry)]
    moment            = time.time() if now is None else now
    blocks: list[str] = []
    if entries:
        lines = "\n".join(f"- {describe_entry(entry, moment)}" for entry in entries)
        blocks.append(
            "你最近说过的关于自己的事（都是你自己说出口的。后面保持一致，不要说出相矛盾的经历；"
            "和作息底色冲突时以这里为准；被问到或正好相关时顺着说，没必要时不复述；"
            "答应的事办完了再说办完）：\n" + lines
        )
    if told:
        lines = "\n".join(f"- {entry.text}" for entry in told)
        blocks.append(
            "你以前讲过的自己的事（讲过就是你的经历：再被问到时还是这个版本，"
            "没人问时不用主动再讲）：\n" + lines
        )
    return "\n".join(blocks)
