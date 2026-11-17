"""冷群时以群友身份主动开口。默认关闭，``proactive.enabled`` 打开后由定时任务驱动。

每次定时检查对每个允许的群判断：在活跃时段内、角色不在睡觉、群里安静了一阵但不是死群、
最后一句不是她自己说的、今天开口次数和间隔都没超、最近没有刚试过一次。满足时把她手里现成的
东西交给聊天模型，让她挑一件对着这个群说；起草后过一遍本地复读检查和发送前自检，自检拦下时
带着理由重写一次，还不通过就不发，并在一段时间内不再尝试。

她手里的东西都来自已有记录：

- 这两天上网看到的、查明白的事（xiaoqing_life 给的候选，带内容本身：讲了什么、里面那句原话、
  链接；这个群聊到她才去搜的会标出来）；
- 今天身上发生的小事和各自的细节；
- 群里其他插件刚推送的内容：全文，有配图时连同图里看得到的东西；
- 这个群和群友的情况（统一记忆里的群页）、更长的一段最近群聊；
- 她前几次在这个群主动说过的话，以及之后有没有人接。

群友到期的事不在这里点名追问，等对方自己开口时由回复顺口关心。发出去的话在投递确认后才记进
聊天记录和节奏记录。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from core.delivery import DeliveryReceipt, ScheduledDelivery
from core.log import LogEvent
from core.plugin_base import segments

from ..chat_runtime import ai_route_context, configured_bot_name, llm_call_config, load_runtime
from ..llm.gateway import LLMError, chat_completions
from ..llm.local_checks import heuristic_check
from ..llm.prompt_builder import build_dialogue_prompt
from ..memory import client as memory_client
from ..memory.local_ids import allocate_local_id
from ..message_parts import build_text_message_parts, render_stored_message
from ..persona import SCOPE_CHAT
from ..planning.action_history import ActionRecord
from ..post_reply_tasks import spawn_life_extraction
from ..store_binding import bound_state
from ..task_scheduler import schedule_action_history_flush, schedule_memory_persist
from ..utils.json_parsing import parse_first_json_object
from ..utils.wall_clock import describe_now, local_datetime
from .daily_life import day_key, today_block
from .online import fetch_share_candidates
from .proactive_state import ProactiveRecord
from .push_log import look_at_images
from .schedule import parse_clock, routine_slot_now, state_text
from .self_timeline import self_timeline_block

log = logging.getLogger(__name__)

_NOTICE_WINDOW_SECONDS = 6 * 3600.0
_NOTICE_CHARS          = 1200
_MAX_NOTICES           = 3
_HISTORY_ITEMS         = 60
DIALOGUE_ITEMS         = 12
_GROUP_DIALOGUE_ITEMS  = 40
_GROUP_DIALOGUE_CHARS  = 3000
_MAX_TEXT_CHARS        = 300
_DRAFT_ATTEMPTS        = 2
_FEEDBACK_SECONDS      = 3 * 86400.0
_FEEDBACK_LINES        = 3
_REPLY_WINDOW_SECONDS  = 3 * 3600.0
_REPLY_QUOTE_CHARS     = 40
_OWN_SOURCES           = frozenset({"", "xiaoqing_chat"})
_MARKER_RE             = re.compile(r"\[[^\]]{0,40}\]")

_DRAFT_INSTRUCTION = """这不是有人在跟你说话，是你自己想在群里说点什么。群里这会儿没人说话。
下面是你手里现成的东西：这两天上网看到的和查明白的事，今天身上发生的小事，群里刚推送的内容；还有这个群最近聊了什么、群友各自在意什么。挑一件你现在最想跟这个群说的，说出来。
群友平时冷不丁冒出一句、别人愿意接，是因为那句话里带着东西：
- 说的是事情本身：是什么，哪一点好玩、离谱或者没想到。记下的原话、数字和细节直接说出来，只说自己看了什么、觉得有意思，别人没法接。值得点开的带上链接。
- 这个群聊过的事有了后续、你后来自己去查了，就接着那个话头说，可以直接叫当时聊这件事的人。
- 想知道大家怎么看、有没有人也遇到过，就问出来。
- 说自己的事时，讲那个让人想吐槽或者有同感的细节。
- 推送的内容里你看出了点什么、有自己的看法或者疑问，就说那一点。
事实只按材料说，材料里没有的经过、数字和原话不补。跟你最近在群里说过的、前几次主动说过的别重样。
材料里一件想说的都挑不出来时才 skip。
只输出一个 JSON 对象：{"decision":"send","text":"要发的话","life_ref":说的是上网看到的事里的第几件（不是就省略）} 或 {"decision":"skip","reason":"一句话原因"}"""

_JUDGE_INSTRUCTION = """现在不是让你写消息，而是替这个角色做一次发送前自检。上面是她的人设。
下面这句话是她准备在群里没人说话时主动发的。判断能不能发：
- 里面的事实（发生过什么、数字、原话、谁聊过什么）都能在材料里找到出处；
- 像她本人在群里随手发的，不像播报、总结或客服；
- 和她最近在群里说过的、前几次主动说过的不是同一件事翻来覆去，和她说过的关于自己的事不矛盾。
你只能放行或拦下，不能改写。只输出 JSON：{"decision":"send","reason":"一句短理由"} 或 {"decision":"drop","reason":"一句短理由"}"""

_TODAY_HEADER  = "今天你身上发生的小事（你自己的生活；括号里是这件事的细节）："
_NOTICE_HEADER = (
    "群里刚推送的内容（别的插件用你的账号发的，群友都看得到，还没人聊过；"
    "“配图里看到的”是那张图上的东西）："
)
_FEEDBACK_HEADER = "你前几次在这个群主动说的话，和之后有没有人接："


def within_active_hours(start: str, end: str, minute_of_day: int) -> bool:
    low, high = parse_clock(start), parse_clock(end)
    if low == high:
        return True
    if low < high:
        return low <= minute_of_day < high
    return minute_of_day >= low or minute_of_day < high


def _own(message: Any) -> bool:
    return message.role == "assistant" and message.source_plugin in _OWN_SOURCES


async def push_notices(
    runtime: Any,
    state: Any,
    chat_id: str,
    history: Sequence[Any],
    *,
    now: float,
    secrets: dict[str, Any],
) -> list[str]:
    """她还没接过话的推送（最近几小时、最后几条）：全文，有配图时连同图里看得到的东西。"""

    spoke_at = max((float(m.ts or 0.0) for m in history if _own(m)), default=0.0)
    pushes = await asyncio.to_thread(
        state.push_log.since, chat_id, max(spoke_at, now - _NOTICE_WINDOW_SECONDS)
    )
    notices: list[str] = []
    for push in pushes[-_MAX_NOTICES:]:
        note = push.image_note
        if push.images and not note:
            note = await look_at_images(push, secrets=secrets, llm=runtime.cfg.llm_config("vision"))
            if note:
                await asyncio.to_thread(state.push_log.set_image_note, chat_id, push, note)
        line = f"（{push.source} 推送）{push.text[:_NOTICE_CHARS]}"
        notices.append(f"{line}\n配图里看到的：{note}" if note else line)
    return notices


def clean_text(text: str) -> str:
    cleaned = _MARKER_RE.sub("", str(text or "")).strip().strip("“”\"'")
    return cleaned[:_MAX_TEXT_CHARS].strip()


def parse_draft(raw: str) -> str:
    data = parse_first_json_object(raw) or {}
    if str(data.get("decision") or "").lower() != "send":
        return ""
    return clean_text(str(data.get("text") or ""))


def parse_verdict(raw: str) -> tuple[bool, str]:
    """自检的结论和一句理由。"""

    data = parse_first_json_object(raw) or {}
    return str(data.get("decision") or "").lower() == "send", str(data.get("reason") or "")[:120]


_SHARE_HEADER = (
    "你这两天上网看到的、查明白的事（都是真实记录，引号里是原话）。"
    "说的是其中某件时，在 JSON 里加上 life_ref 填它的编号："
)
_SHARE_HEADER_GROUP = (
    "你这两天上网看到的、查明白的事（都是真实记录，引号里是原话；"
    "写着“听群里在聊…才去搜的”是这个群聊到、你后来去看的）。"
    "说的是其中某件时，在 JSON 里加上 life_ref 填它的编号："
)


def share_block(lines: Sequence[str], *, group: bool = False) -> str:
    header = _SHARE_HEADER_GROUP if group else _SHARE_HEADER
    return "\n".join([header, *(f"{n}. {line}" for n, line in enumerate(lines, start=1))])


def parse_life_ref(raw: str, candidates: Sequence[dict[str, Any]]) -> list[str]:
    """草稿 JSON 里的 life_ref（1 起的编号）对应的经历 ID；没写或越界就是空。"""

    ref = (parse_first_json_object(raw) or {}).get("life_ref")
    if isinstance(ref, bool) or not isinstance(ref, int) or not 1 <= ref <= len(candidates):
        return []
    return [str(candidates[ref - 1]["episode_id"])]


def _day_label(ts: float, now: float) -> str:
    at, today = local_datetime(ts), local_datetime(now)
    gap = (today.date() - at.date()).days
    day = {0: "今天", 1: "昨天", 2: "前天"}.get(gap, f"{at.month}月{at.day}日")
    return f"{day} {at:%H:%M}"


def feedback_block(record: ProactiveRecord, history: Sequence[Any], *, now: float) -> str:
    """她前几次主动说的话，各自之后有没有人接；聊天记录已经翻不到的那几次只列原话。"""

    rows = [(ts, text) for ts, text in record.recent if 0 <= now - ts <= _FEEDBACK_SECONDS]
    if not rows:
        return ""
    oldest = min((float(m.ts or 0.0) for m in history), default=now)
    lines = []
    for ts, text in rows[-_FEEDBACK_LINES:]:
        outcome = ""
        if ts >= oldest:
            reply = next(
                (
                    m
                    for m in history
                    if m.role == "user" and ts < float(m.ts or 0.0) <= ts + _REPLY_WINDOW_SECONDS
                ),
                None,
            )
            outcome = (
                f"（{reply.name or '有人'}接了：{render_stored_message(reply).strip()[:_REPLY_QUOTE_CHARS]}）"
                if reply is not None
                else "（没人接）"
            )
        lines.append(f"- {_day_label(ts, now)}：{text}{outcome}")
    return "\n".join([_FEEDBACK_HEADER, *lines])


def _materials(*, now: float, idle_minutes: int, sections: Sequence[str], dialogue: str) -> str:
    """起草和自检看到的全部材料：此刻、她手里的各段东西（空的略过）、最近的群聊。"""

    head = f"现在：{describe_now(now)}，群里已经安静了约{idle_minutes}分钟。"
    tail = "最近的群聊：\n" + (dialogue or "（无）")
    return "\n\n".join([head, *(section for section in sections if section), tail])


async def _complete(route: str, secrets: dict[str, Any], llm: Any, system: str, user: str) -> str:
    return await chat_completions(
        route    = route,
        secrets  = secrets,
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **llm.as_kwargs(),
    )


def build_receipt(
    context: Any,
    runtime: Any,
    state: Any,
    *,
    chat_id: str,
    bot_name: str,
    text: str,
    secrets: dict[str, Any],
    state_key: str                                  = "",
    shared_life_ids: Sequence[str]                  = (),
    reasoning: str                                  = "group_idle",
    on_commit: Callable[[], Awaitable[None]] | None = None,
) -> DeliveryReceipt:
    async def commit() -> None:
        now = time.time()
        state.memory_store.append(
            chat_id,
            role          = "assistant",
            name          = bot_name,
            local_id      = allocate_local_id(chat_id),
            parts         = build_text_message_parts(text),
            source_plugin = "xiaoqing_chat",
            message_ids   = receipt.message_ids,
        )
        schedule_memory_persist(context, runtime, chat_id=chat_id)
        state.set_last_reply_ts(chat_id, now)
        if on_commit is not None:
            await on_commit()
        await asyncio.to_thread(
            state.proactive_state.record,
            state_key or chat_id,
            ts              = now,
            day             = day_key(local_datetime(now)),
            shared_life_ids = shared_life_ids,
            text            = text,
        )
        state.action_history.append(
            chat_id,
            ActionRecord(
                ts           = now,
                local_target = "",
                action       = "proactive_message",
                reasoning    = reasoning,
                detail       = {"text": text[:200]},
                executed     = True,
            ),
        )
        schedule_action_history_flush(context, runtime, chat_id=chat_id)
        history = list(await state.memory_store.get_recent_async(chat_id, max_items=DIALOGUE_ITEMS))
        spawn_life_extraction(
            context,
            runtime,
            state,
            chat_id  = chat_id,
            bot_name = bot_name,
            secrets  = secrets,
            history  = history,
            source   = "proactive",
        )

    async def rollback() -> None:
        log.info(LogEvent("proactive.delivery_failed", chat_id=chat_id))

    receipt = DeliveryReceipt(expected_actions=1, commit=commit, rollback=rollback, unknown=commit)
    return receipt


async def draft_through_judge(
    runtime: Any,
    *,
    chat_id: str,
    secrets: dict[str, Any],
    system: str,
    material: str,
    history: Sequence[Any],
    judge_system: str,
) -> tuple[str, str] | None:
    """起草 → 本地复读检查 → 发送前自检；返回 (草稿, 模型原始输出)，不通过返回 None。

    自检拦下时把理由交回去重写一次（多数是一个说过头的细节或一个不准的时间词）；第二稿还不过，
    或者她自己选择不说、和最近说过的话重复，这一次就不开口。
    """

    check = runtime.cfg.reply_check
    retry = ""
    for _ in range(_DRAFT_ATTEMPTS):
        try:
            raw = await _complete(
                "chat",
                secrets,
                llm_call_config(runtime.cfg, foreground=True),
                system,
                material + retry,
            )
            draft = parse_draft(raw)
        except LLMError:
            log_skip(chat_id, "draft_failed")
            return None
        if not draft:
            log_skip(chat_id, "model_skip")
            return None
        if heuristic_check(
            reply                = draft,
            history              = history,
            max_repeat_compare   = check.max_repeat_compare,
            similarity_threshold = check.similarity_threshold,
            max_assistant_in_row = check.max_assistant_in_row,
        ):
            log_skip(chat_id, "local_check")
            return None
        if not runtime.cfg.proactive.enable_persona_judge:
            return draft, raw
        try:
            allowed, reason = parse_verdict(
                await _complete(
                    "checker",
                    secrets,
                    runtime.cfg.llm_config("checker"),
                    judge_system,
                    f"{material}\n\n她准备发：{draft}",
                )
            )
        except LLMError:
            allowed, reason = False, ""
        if allowed:
            return draft, raw
        if not reason:
            break
        retry = f"\n\n你刚才想发：{draft}\n发之前自己看了一遍，觉得不行：{reason}\n改掉这一点重说，或者换一件事说。"
    log_skip(chat_id, "judge")
    return None


def log_skip(chat_id: str | None, code: str, **numbers: float) -> None:
    """记一次没开口及原因，排查“她怎么一直不说话”时能直接看到卡在哪一步。"""

    log.info(
        LogEvent(
            "proactive.skip",
            chat_id     = chat_id,
            reason_code = code,
            **{key: round(value, 1) for key, value in numbers.items()},
        )
    )


def _target_groups(context: Any, configured: Sequence[int]) -> list[int]:
    groups = [int(group) for group in context.default_groups()]
    if configured:
        allowed = set(configured)
        groups  = [group for group in groups if group in allowed]
    return groups


async def _consider_group(
    context: Any, runtime: Any, state: Any, group_id: int, *, now: float, life_text: str
) -> ScheduledDelivery | None:
    cfg     = runtime.cfg.proactive
    chat_id = f"g{group_id}"
    record  = state.proactive_state.get(chat_id)
    today   = day_key(local_datetime(now))
    if record.count_on(today) >= cfg.max_per_group_per_day:
        log_skip(chat_id, "daily_cap")
        return None
    if now - record.last_ts < cfg.min_interval_hours * 3600.0:
        log_skip(chat_id, "interval", minutes_since=(now - record.last_ts) / 60)
        return None
    if now - record.last_attempt_ts < cfg.retry_after_skip_minutes * 60.0:
        # 刚试过一次没发出去：群里情况没变时再问一遍模型也只是同样的结论。
        log_skip(chat_id, "recent_attempt")
        return None
    history = list(await state.memory_store.get_recent_async(chat_id, max_items=_HISTORY_ITEMS))
    if not history:
        log_skip(chat_id, "no_history")
        return None
    last = history[-1]
    idle = now - float(last.ts or 0.0)
    if idle < cfg.min_group_idle_minutes * 60.0:
        log_skip(chat_id, "not_idle", idle_minutes=idle / 60)
        return None
    if idle > cfg.max_group_dormant_hours * 3600.0:
        log_skip(chat_id, "dormant", idle_minutes=idle / 60)
        return None
    # Agent 交付也算自己的末句；通用聊天轮次和推送材料分类保持各自语义。
    if last.role == "assistant" and last.source_plugin in _OWN_SOURCES | {"xiaoqing_agent"}:
        # 最后一句是她自己说的，没人接；再开口就成了自言自语。
        log_skip(chat_id, "own_last_word")
        return None
    secrets = ai_route_context(context, chat_id=chat_id)
    if secrets.get("_ai") is None:
        log_skip(chat_id, "ai_unavailable")
        return None

    bot_name = configured_bot_name(context)
    life     = runtime.cfg.life
    moment   = local_datetime(now)
    recalled = await memory_client.recall(
        context,
        purpose    = "proactive",
        chat_id    = chat_id,
        history    = history,
        self_limit = int(life.self_timeline_max_injected),
    )
    # 这个群还记着的话题词交过去：对方据此标出哪些事是这个群聊到她才去搜的。
    candidates = await fetch_share_candidates(
        context, runtime, exclude_ids=record.shared_life_ids, topics=recalled.topics
    )
    notices = await push_notices(runtime, state, chat_id, history, now=now, secrets=secrets)
    sections = [
        # 可以说的事
        share_block([c["line"] for c in candidates], group=True) if candidates else "",
        today_block(state.daily_life.get(day_key(moment)), moment, header=_TODAY_HEADER)
        if life.enable_daily_life
        else "",
        _NOTICE_HEADER + "\n" + "\n".join(notices) if notices else "",
        # 说的时候要对得上的：此刻的作息、她自己说过的事、这个群的情况、前几次说过什么
        life_text,
        # 分享候选里已经有这几条经历的细节：自我页里只引用它们的行不再放。
        recalled.self_page(c["episode_id"] for c in candidates)
        if life.enable_self_timeline
        else "",
        self_timeline_block(recalled.self_entries, now=now) if life.enable_self_timeline else "",
        recalled.block("group"),
        feedback_block(record, history, now=now),
    ]
    card    = runtime.cfg.personality.card
    persona = "\n\n".join(
        part for part in (card.facts(bot_name), card.scoped(SCOPE_CHAT, bot_name)) if part
    )
    dialogue = build_dialogue_prompt(
        history[-_GROUP_DIALOGUE_ITEMS:],
        bot_name  = bot_name,
        truncate  = True,
        max_chars = _GROUP_DIALOGUE_CHARS,
        max_items = _GROUP_DIALOGUE_ITEMS,
    )
    material = _materials(
        now=now, idle_minutes=int(idle // 60), sections=sections, dialogue=dialogue
    )
    await asyncio.to_thread(state.proactive_state.record_attempt, chat_id, ts=now)
    done = await draft_through_judge(
        runtime,
        chat_id      = chat_id,
        secrets      = secrets,
        system       = f"{persona}\n\n{_DRAFT_INSTRUCTION}",
        material     = material,
        history      = history,
        judge_system = f"{card.facts(bot_name)}\n\n{_JUDGE_INSTRUCTION}",
    )
    if done is None:
        return None
    draft, raw_draft = done
    log.info(LogEvent("proactive.send", chat_id=chat_id, text=draft))
    receipt = build_receipt(
        context,
        runtime,
        state,
        chat_id         = chat_id,
        bot_name        = bot_name,
        text            = draft,
        secrets         = secrets,
        shared_life_ids = parse_life_ref(raw_draft, candidates),
    )
    return ScheduledDelivery.group(group_id, segments(draft), receipt=receipt)


async def run_proactive(context: Any) -> list[ScheduledDelivery]:
    """一次定时检查；返回要发的群消息（通常为空）。"""

    runtime = load_runtime(context)
    cfg     = runtime.cfg.proactive
    if not cfg.enabled or not runtime.cfg.enable_smalltalk:
        return []
    now    = time.time()
    moment = local_datetime(now)
    if not within_active_hours(
        cfg.active_hours_start, cfg.active_hours_end, moment.hour * 60 + moment.minute
    ):
        log_skip(None, "outside_active_hours")
        return []
    try:
        slot = routine_slot_now(runtime.cfg.personality.card, moment)
    except ValueError:
        slot = None
    if slot is not None and slot.availability == "asleep":
        log_skip(None, "asleep")
        return []
    if slot is not None and slot.availability == "busy" and random.random() < 0.5:
        log_skip(None, "busy")
        return []
    life_text = state_text(slot) if runtime.cfg.life.enable_schedule_state else ""
    state     = bound_state(context)
    groups    = _target_groups(context, cfg.group_ids)
    if not groups:
        log_skip(None, "no_target_group")
    deliveries: list[ScheduledDelivery] = []
    for group_id in groups:
        delivery = await _consider_group(
            context, runtime, state, group_id, now=now, life_text=life_text
        )
        if delivery is not None:
            deliveries.append(delivery)
    if cfg.private_user_ids and not (slot is not None and slot.availability != "free"):
        from .proactive_private import consider_private

        for user_id in cfg.private_user_ids:
            private = await consider_private(
                context, runtime, state, user_id, now=now, life_text=life_text
            )
            if private is not None:
                deliveries.append(private)
    return deliveries
