"""投递后的生活记录抽取：一次调用同时维护角色的自我陈述和群友的未完话头。

输入是角色刚刚发出的一条发言（聊天回复、agent 回复或后台结果、主动开口）和它之前的几条
对话。模型只能新增有原话依据的记录，或结束材料里已有的记录；ID、会话归属、过期时间和
发言人都由这里校验，模型给出的说话人必须出现在对话行里。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from weakref import WeakValueDictionary

from ..llm.gateway import LLMError, chat_completions
from ..llm.llm_config import LLMCallConfig
from ..message_parts import render_stored_message
from ..utils.json_parsing import parse_first_json_object
from ..utils.wall_clock import describe_now, local_datetime
from .open_loops import OpenLoop, same_topic
from .self_timeline import SELF_KINDS, SelfEntry, new_entry

_DIALOGUE_ITEMS = 8
_LINE_CHARS     = 200
_MAX_NEW_SELF   = 4
_MAX_NEW_LOOPS  = 3
_LOOP_HORIZON_S = 60 * 86400.0
_TIME_CUE_RE    = re.compile(
    r"明天|后天|大后天|下周|下个?月|周[一二三四五六日天末]|星期[一二三四五六日天]|今晚|今天下午|"
    r"待会|一会儿?|考试|期中|期末|面试|答辩|比赛|体检|复诊|出发|航班|高铁|ddl|DDL|截止|\d+[号日]"
)
# 打算和承诺：漏记会让她忘了自己答应过什么，每条都值得读一遍。
_COMMITMENT_CUE_RE = re.compile(
    r"答应|帮你|给你|发你|告诉你|回头|待会|等会|一会|晚点|明天|今晚|晚上|周末|下次|"
    r"周[一二三四五六日天]|打算|准备|要去|得去|约了"
)
# 近况：说自己近况时常省略“我”（“刚下课，正往食堂冲”）。闲聊里到处都是，同一会话限频读取。
_STATE_CUE_RE = re.compile(
    r"我|咱|刚|正在|正往|还在|在(?:宿舍|食堂|图书馆|教室|实验室|路上|床上|上课|吃)|下课|上课|起床|"
    r"吃饭|睡觉|作业|实验|报告|ddl|DDL|困|累|饿"
)
# 没有第一人称也可能是在交差：“照片整理好了”。
_DONE_RE = re.compile(r"好了|好啦|弄完|搞定|做完|办完|写完|查到|查完|整理好|发你|发给你|给你了")
_chat_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# 会话 → 上次因为近况线索而调用抽取的时间。只在进程内限频，重启后从头算。
_last_state_read: dict[str, float] = {}


def _canned_replies() -> frozenset[str]:
    from ..reply_fallbacks import ALL_FALLBACK_TEXTS

    return ALL_FALLBACK_TEXTS | {"消息有点多，我缓一下再回"}


_SYSTEM = """你在帮一个聊天角色整理两份记录，只输出一个 JSON 对象。

一、self：角色（{bot_name}）在“本次发言”里说出口的、关于她自己的事。
- kind=state：她此刻或刚才在做什么、身体或心情（如“在食堂吃糖醋里脊”“刚下课”“有点困”）。
- kind=plan：她自己接下来要做的事（如“晚上去天文社观测”）。
- kind=promise：她答应对方要做的事（如“弄完告诉你”“明天帮你查”），counterpart 写答应了谁。
- shareable：这条只讲她自己的活动或状态、没涉及对方、也没透露你们聊过什么时为 true，她在别的聊天里也会记得；涉及对方或你们之间的事为 false。
- 只记她当真说出的事。玩笑、反问、假设、引用或转述别人的话、角色扮演、评价别人、泛泛的观点都不记。
- text 是省略主语的短语，不超过 30 字。hours 是这条大约多少小时后不再成立。
- 已有记录里说过的同一件事不要重复记。

二、resolved_self：已有自我记录里，本次发言表明已经做完、取消或被新情况取代的条目，给出 id 和 status（done 或 dropped）。

三、loops：近期对话里，群友说到的、发生在他们自己身上、有明确时间点、之后值得关心结果的事（考试、面试、答辩、比赛、体检、出行、交作业）。
- user_id 必须是对话行里那个人的 <QQ号>；转述别人的事不记，角色自己的事不记。
- due 是这件事发生或截止的当地时间，格式 YYYY-MM-DDTHH:MM，按“现在”换算“明天”“下周三”；只知道日期时用当天 18:00。
- title 不超过 20 字，如“数电期末考试”。日常小事和没有时间点的事不记。

四、resolved_loops：已有未完话头里，对方已经说了结果、取消了或不想再提的 id。

五、asked_loops：已有未完话头里，角色在“本次发言”中问起或关心了的 id（换了说法也算）。

没有内容的字段输出空数组。格式：
{{"self":[{{"kind":"state","text":"...","hours":2,"counterpart":"","shareable":true}}],"resolved_self":[{{"id":"...","status":"done"}}],"loops":[{{"user_id":123,"user_name":"...","title":"...","due":"2026-01-01T09:00"}}],"resolved_loops":["..."],"asked_loops":["..."]}}
"""


def _dialogue_lines(history: Sequence[Any], bot_name: str) -> tuple[list[str], str, dict[int, str]]:
    """把本次发言之前的几条对话写成行，返回（对话行，本次发言，可信说话人）。"""

    if not history or history[-1].role != "assistant":
        return [], "", {}
    current                  = render_stored_message(history[-1]).strip()[: _LINE_CHARS * 3]
    lines   : list[str]      = []
    speakers: dict[int, str] = {}
    for message in history[-1 - _DIALOGUE_ITEMS : -1]:
        text = render_stored_message(message).strip()[:_LINE_CHARS]
        if not text:
            continue
        if message.role == "user" and message.user_id:
            name                           = (message.name or "用户").strip() or "用户"
            speakers[int(message.user_id)] = name
            lines.append(f"{name}<{int(message.user_id)}>：{text}")
        elif message.role == "assistant":
            lines.append(f"{bot_name}：{text}")
    return lines, current, speakers


def worth_extracting(
    *,
    reply: str,
    dialogue: Sequence[str],
    open_self: Sequence[SelfEntry],
    open_loops: Sequence[OpenLoop],
    self_enabled: bool,
    loops_enabled: bool,
    state_read_due: bool = True,
) -> bool:
    """没有任何可能产生记录的线索时省掉这次调用。

    打算、承诺和交差的线索每次都读；只有近况线索时，``state_read_due`` 为假就跳过。
    """

    if self_enabled:
        if _COMMITMENT_CUE_RE.search(reply) or (open_self and _DONE_RE.search(reply)):
            return True
        if state_read_due and _STATE_CUE_RE.search(reply):
            return True
    # ``open_loops`` 只含这段对话里说话人的话头：对方可能正在说结果，或者角色正在问。
    if loops_enabled and (open_loops or any(_TIME_CUE_RE.search(line) for line in dialogue[-4:])):
        return True
    return False


def build_messages(
    *,
    bot_name: str,
    dialogue: Sequence[str],
    reply: str,
    open_self: Sequence[SelfEntry],
    open_loops: Sequence[OpenLoop],
    now: float,
) -> list[dict[str, str]]:
    known_self  = [{"id": e.entry_id, "kind": e.kind, "text": e.text} for e in open_self]
    known_loops = [
        {"id": loop.loop_id, "user_id": loop.user_id, "title": loop.title} for loop in open_loops
    ]
    user = "\n".join(
        [
            f"现在：{describe_now(now)}",
            "已有自我记录：" + json.dumps(known_self, ensure_ascii=False),
            "已有未完话头：" + json.dumps(known_loops, ensure_ascii=False),
            "近期对话（不含本次发言）：",
            *(dialogue or ["（无）"]),
            f"{bot_name}（本次发言）：{reply}",
        ]
    )
    return [
        {"role": "system", "content": _SYSTEM.format(bot_name=bot_name)},
        {"role": "user", "content": user},
    ]


def _parse_due(value: Any, now: float) -> float | None:
    text = str(value or "").strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=local_datetime(now).tzinfo)
    ts = moment.timestamp()
    if not now - 2 * 86400 <= ts <= now + _LOOP_HORIZON_S:
        return None
    return ts


def _list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def parse_result(
    raw: str,
    *,
    chat_id: str,
    source: str,
    open_self: Sequence[SelfEntry],
    open_loops: Sequence[OpenLoop],
    speakers: dict[int, str],
    now: float,
) -> tuple[list[SelfEntry], list[tuple[str, str]], list[dict[str, Any]], set[str], set[str]]:
    """校验模型输出：（新自我记录，结束的自我记录，新话头，结束的话头 ID，问过的话头 ID）。"""

    data                   = parse_first_json_object(raw) or {}
    added: list[SelfEntry] = []
    for item in _list(data, "self")[:_MAX_NEW_SELF]:
        if not isinstance(item, dict) or item.get("kind") not in SELF_KINDS:
            continue
        # 私聊里说的近况默认只在这个私聊里记得，模型确认不涉及对方时才各处共用。
        shared = not chat_id.startswith("u") or item.get("shareable") is True
        entry  = new_entry(
            shared      = shared,
            chat_id     = chat_id,
            kind        = str(item.get("kind")),
            text        = str(item.get("text") or ""),
            hours       = item.get("hours"),
            source      = source,
            counterpart = str(item.get("counterpart") or ""),
            now         = now,
        )
        # 同类、意思差不多的记录已经有了（“刚摸鱼”和“现在在摸鱼”），不再重复记。
        if entry is not None and not any(
            known.kind == entry.kind and same_topic(known.text, entry.text)
            for known in (*open_self, *added)
        ):
            added.append(entry)

    known_self_ids                       = {entry.entry_id for entry in open_self}
    resolved_self: list[tuple[str, str]] = []
    for item in _list(data, "resolved_self"):
        if isinstance(item, dict) and str(item.get("id")) in known_self_ids:
            status = str(item.get("status") or "done")
            resolved_self.append((str(item.get("id")), status if status == "dropped" else "done"))

    loops: list[dict[str, Any]] = []
    for item in _list(data, "loops")[:_MAX_NEW_LOOPS]:
        if not isinstance(item, dict):
            continue
        try:
            user_id = int(str(item.get("user_id")))
        except (TypeError, ValueError):
            continue
        due   = _parse_due(item.get("due"), now)
        title = " ".join(str(item.get("title") or "").split())
        if user_id not in speakers or due is None or not title:
            continue
        loops.append(
            {"user_id": user_id, "user_name": speakers[user_id], "title": title, "due_at": due}
        )

    known_loop_ids = {loop.loop_id for loop in open_loops}
    closed = {str(item) for item in _list(data, "resolved_loops") if str(item) in known_loop_ids}
    asked = {
        str(item) for item in _list(data, "asked_loops") if str(item) in known_loop_ids
    } - closed
    return added, resolved_self, loops, closed, asked


def _chat_lock(chat_id: str) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock                 = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


async def extract_life_records(
    *,
    state: Any,
    life_cfg: Any,
    secrets: dict[str, Any],
    llm: LLMCallConfig,
    chat_id: str,
    bot_name: str,
    history: Sequence[Any],
    source: str,
) -> bool:
    """对最新一条角色发言做一次抽取并写入；没有调用模型时返回 False。

    同一会话的抽取串行执行，后一条发言能看到前一条写入的记录。
    """

    self_enabled  = bool(life_cfg.enable_self_timeline)
    loops_enabled = bool(life_cfg.enable_open_loops)
    if not (self_enabled or loops_enabled) or secrets.get("_ai") is None:
        return False
    dialogue, reply, speakers = _dialogue_lines(history, bot_name)
    if not reply or reply.strip() in _canned_replies():
        # 审查耗尽后的兜底句不是她真想说的话，不能当成近况记下来。
        return False
    async with _chat_lock(chat_id):
        now        = time.time()
        generation = await asyncio.to_thread(state.open_loops.generation, chat_id)
        open_self = await asyncio.to_thread(state.self_timeline.visible, chat_id, now=now, limit=20)
        open_loops = (
            [
                loop
                for loop in await asyncio.to_thread(state.open_loops.pending, chat_id, now=now)
                if loop.user_id in speakers
            ]
            if loops_enabled
            else []
        )
        if not self_enabled:
            open_self = []
        interval       = float(getattr(life_cfg, "self_state_extract_interval_seconds", 0.0))
        state_read_due = now - _last_state_read.get(chat_id, 0.0) >= interval
        if not worth_extracting(
            reply          = reply,
            dialogue       = dialogue,
            open_self      = open_self,
            open_loops     = open_loops,
            self_enabled   = self_enabled,
            loops_enabled  = loops_enabled,
            state_read_due = state_read_due,
        ):
            return False
        _last_state_read[chat_id] = now
        try:
            raw = await chat_completions(
                route    = "reasoning",
                secrets  = secrets,
                messages = build_messages(
                    bot_name   = bot_name,
                    dialogue   = dialogue,
                    reply      = reply,
                    open_self  = open_self,
                    open_loops = open_loops,
                    now        = now,
                ),
                **llm.as_kwargs(),
            )
        except LLMError:
            return True
        added, resolved, loops, closed, asked = parse_result(
            raw,
            chat_id    = chat_id,
            source     = source,
            open_self  = open_self,
            open_loops = open_loops,
            speakers   = speakers,
            now        = now,
        )

        def commit() -> None:
            # 抽取期间会话被重置时，旧对话里的记录不能写回来。
            with state.open_loops.generation_guard(chat_id, generation) as current:
                if not current:
                    return
                if self_enabled and (added or resolved):
                    state.self_timeline.apply(
                        added       = added,
                        resolved    = resolved,
                        chat_id     = chat_id,
                        max_entries = int(life_cfg.self_timeline_max_entries),
                        now         = now,
                    )
                if not loops_enabled:
                    return
                for loop in loops:
                    state.open_loops.add(
                        chat_id, max_loops=int(life_cfg.open_loop_max_per_chat), now=now, **loop
                    )
                state.open_loops.close(chat_id, closed, now=now)
                for loop_id in asked:
                    state.open_loops.mark_asked(chat_id, loop_id, now=now)

        await asyncio.to_thread(commit)
    return True
