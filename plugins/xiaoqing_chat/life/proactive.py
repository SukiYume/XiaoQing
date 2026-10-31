"""冷群时以群友身份主动开口。默认关闭，``proactive.enabled`` 打开后由定时任务驱动。

每次定时检查对每个允许的群判断：在活跃时段内、角色不在睡觉、群里安静了一阵但不是死群、
最后一句不是她自己说的、今天开口次数和间隔都没超、最近没有刚试过一次。满足时把材料交给
聊天模型起草，模型默认可以不说（skip）；起草后过一遍本地复读检查和人设自检，任何一步不
通过都不发，并在一段时间内不再尝试。

材料只来自已有记录：群里最近的插件推送（APOD、arXiv 等）和她自己的作息与近况。群友到期
的事不在这里点名追问，等对方自己开口时由回复顺口关心。发出去的话在投递确认后才记进聊天
记录和节奏记录。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Sequence
from typing import Any

from core.delivery import DeliveryReceipt, ScheduledDelivery
from core.plugin_base import segments

from ..chat_runtime import ai_route_context, configured_bot_name, llm_call_config, load_runtime
from ..llm.gateway import LLMError, chat_completions
from ..llm.local_checks import heuristic_check
from ..llm.prompt_builder import build_dialogue_prompt
from ..logging_utils import log_step
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
from .schedule import routine_slot_now, state_text
from .self_timeline import self_timeline_block

_NOTICE_WINDOW_SECONDS = 6 * 3600.0
_NOTICE_CHARS          = 400
_DIALOGUE_ITEMS        = 12
_MAX_TEXT_CHARS        = 120
_OWN_SOURCES           = frozenset({"", "xiaoqing_chat"})
_MARKER_RE             = re.compile(r"\[[^\]]{0,40}\]")

_DRAFT_INSTRUCTION = """这是一次主动开口的机会，不是有人在跟你说话。群里已经安静了一阵。
你是群里的普通群友，判断现在有没有一句自然、值得说的话：
- 可以就群里刚推送过的内容说说自己的看法或感受，也可以随手分享一件跟你兴趣有关的小事或此刻的状态。不点名追问某个群友。
- 像群友随手发的一两句，没人接话也成立。不要“大家好”“有人在吗”这类签到式开场，不要说群里好安静、好久没人说话，不要求别人回复，不要说你一直记着谁的事。
- 群友的事只按材料说；你自己的近况可以是模糊的小事，但不能和“你最近说过的关于自己的事”矛盾。
- 没有自然的由头，或者要说的和最近聊天重复时，选择 skip。拿不准就 skip。
- 不要写表情标记、方括号或引号包住的整句。
只输出一个 JSON 对象：{"decision":"send","text":"要发的话"} 或 {"decision":"skip","reason":"no_natural_opening"}"""

_JUDGE_INSTRUCTION = """现在不是让你写消息，而是替这个角色做一次发送前自检。上面是她的人设。
下面这句话是她准备在群里安静一阵后主动发的。判断能不能发：
- 符合人设和群友身份，像真人随手发的，而不是签到、客服、播报或总结；
- 和最近的聊天接得上，不重复她最近说过的话，不翻已经聊完的事；
- 不编造群友的经历，不评判别人的习惯，不催人回复，不暗示自己一直在等或一直记着；
- 与她最近说过的关于自己的事不矛盾。
你只能放行或拦下，不能改写。只输出 JSON：{"decision":"send","reason":"一句短理由"} 或 {"decision":"drop","reason":"一句短理由"}"""


def _clock_minutes(value: str) -> int:
    hours, minutes = (int(part) for part in value.split(":"))
    return hours * 60 + minutes


def within_active_hours(start: str, end: str, minute_of_day: int) -> bool:
    low, high = _clock_minutes(start), _clock_minutes(end)
    if low == high:
        return True
    if low < high:
        return low <= minute_of_day < high
    return minute_of_day >= low or minute_of_day < high


def plugin_notices(history: Sequence[Any], *, now: float) -> list[str]:
    """最近几小时群里其他插件的推送，角色还没接过话的那些。"""

    notices: list[str] = []
    for message in reversed(history):
        if message.role == "assistant" and message.source_plugin in _OWN_SOURCES:
            break
        if now - float(message.ts or 0.0) > _NOTICE_WINDOW_SECONDS:
            break
        if message.role == "assistant" and message.source_plugin not in _OWN_SOURCES | {
            "xiaoqing_agent"
        }:
            text = render_stored_message(message).strip()
            if text:
                notices.append(f"（{message.source_plugin} 推送）{text[:_NOTICE_CHARS]}")
    return list(reversed(notices[:2]))


def clean_text(text: str) -> str:
    cleaned = _MARKER_RE.sub("", str(text or "")).strip().strip("“”\"'")
    return cleaned[:_MAX_TEXT_CHARS].strip()


def parse_draft(raw: str) -> str:
    data = parse_first_json_object(raw) or {}
    if str(data.get("decision") or "").lower() != "send":
        return ""
    return clean_text(str(data.get("text") or ""))


def parse_judgement(raw: str) -> bool:
    data = parse_first_json_object(raw) or {}
    return str(data.get("decision") or "").lower() == "send"


def _materials(
    *,
    notices: Sequence[str],
    life_text: str,
    idle_minutes: int,
    dialogue: str,
    now: float,
) -> str:
    lines = [f"现在：{describe_now(now)}，群里已经安静了约{idle_minutes}分钟。"]
    if life_text:
        lines.append(life_text)
    if notices:
        lines.append("群里最近的插件推送（还没人跟你聊过）：\n" + "\n".join(notices))
    lines.append("最近的群聊：\n" + (dialogue or "（无）"))
    return "\n\n".join(lines)


async def _complete(route: str, secrets: dict[str, Any], llm: Any, system: str, user: str) -> str:
    return await chat_completions(
        route    = route,
        secrets  = secrets,
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}],
        **llm.as_kwargs(),
    )


def _receipt(
    context: Any,
    runtime: Any,
    state: Any,
    *,
    chat_id: str,
    bot_name: str,
    text: str,
    secrets: dict[str, Any],
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
        await asyncio.to_thread(
            state.proactive_state.record,
            chat_id,
            ts  = now,
            day = local_datetime(now).strftime("%Y-%m-%d"),
        )
        state.action_history.append(
            chat_id,
            ActionRecord(
                ts           = now,
                local_target = "",
                action       = "proactive_message",
                reasoning    = "group_idle",
                detail       = {"text": text[:200]},
                executed     = True,
            ),
        )
        schedule_action_history_flush(context, runtime, chat_id=chat_id)
        history = list(
            await state.memory_store.get_recent_async(chat_id, max_items=_DIALOGUE_ITEMS)
        )
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
        log_step(context, runtime, chat_id=chat_id, step="proactive.delivery_failed", fields={})

    receipt = DeliveryReceipt(expected_actions=1, commit=commit, rollback=rollback, unknown=commit)
    return receipt


def _skip(context: Any, runtime: Any, chat_id: str, code: str, **numbers: float) -> None:
    """记一次没开口及原因，排查“她怎么一直不说话”时能直接看到卡在哪一步。"""

    log_step(
        context,
        runtime,
        chat_id = chat_id,
        step    = "proactive.skip",
        fields  = {"reason_code": code, **{key: round(value, 1) for key, value in numbers.items()}},
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
    today   = local_datetime(now).strftime("%Y-%m-%d")
    if record.count_on(today) >= cfg.max_per_group_per_day:
        _skip(context, runtime, chat_id, "daily_cap")
        return None
    if now - record.last_ts < cfg.min_interval_hours * 3600.0:
        _skip(context, runtime, chat_id, "interval", minutes_since=(now - record.last_ts) / 60)
        return None
    if now - record.last_attempt_ts < cfg.retry_after_skip_minutes * 60.0:
        # 刚试过一次没发出去：群里情况没变时再问一遍模型也只是同样的结论。
        _skip(context, runtime, chat_id, "recent_attempt")
        return None
    history = list(await state.memory_store.get_recent_async(chat_id, max_items=40))
    if not history:
        _skip(context, runtime, chat_id, "no_history")
        return None
    last = history[-1]
    idle = now - float(last.ts or 0.0)
    if idle < cfg.min_group_idle_minutes * 60.0:
        _skip(context, runtime, chat_id, "not_idle", idle_minutes=idle / 60)
        return None
    if idle > cfg.max_group_dormant_hours * 3600.0:
        _skip(context, runtime, chat_id, "dormant", idle_minutes=idle / 60)
        return None
    if last.role == "assistant" and last.source_plugin in _OWN_SOURCES:
        # 最后一句是她自己说的，没人接；再开口就成了自言自语。
        _skip(context, runtime, chat_id, "own_last_word")
        return None

    bot_name = configured_bot_name(context)
    life     = runtime.cfg.life
    blocks   = [life_text] if life_text else []
    if life.enable_daily_life:
        moment = local_datetime(now)
        if block := today_block(state.daily_life.get(day_key(moment)), moment):
            blocks.append(block)
    if life.enable_self_timeline:
        entries = state.self_timeline.visible(chat_id, limit=int(life.self_timeline_max_injected))
        if block := self_timeline_block(entries, now=now):
            blocks.append(block)
    card    = runtime.cfg.personality.card
    persona = "\n\n".join(
        part for part in (card.facts(bot_name), card.scoped(SCOPE_CHAT, bot_name)) if part
    )
    dialogue = build_dialogue_prompt(history[-_DIALOGUE_ITEMS:], bot_name=bot_name, truncate=True)
    material = _materials(
        notices=plugin_notices(history, now=now),
        life_text    = "\n".join(blocks),
        idle_minutes = int(idle // 60),
        dialogue     = dialogue,
        now          = now,
    )
    secrets = ai_route_context(context, chat_id=chat_id)
    if secrets.get("_ai") is None:
        _skip(context, runtime, chat_id, "ai_unavailable")
        return None
    await asyncio.to_thread(state.proactive_state.record_attempt, chat_id, ts=now)
    try:
        draft = parse_draft(
            await _complete(
                "chat",
                secrets,
                llm_call_config(runtime.cfg, foreground=True),
                f"{persona}\n\n{_DRAFT_INSTRUCTION}",
                material,
            )
        )
    except LLMError:
        _skip(context, runtime, chat_id, "draft_failed")
        return None
    if not draft:
        _skip(context, runtime, chat_id, "model_skip")
        return None
    check = runtime.cfg.reply_check
    if heuristic_check(
        reply                = draft,
        history              = history,
        max_repeat_compare   = check.max_repeat_compare,
        similarity_threshold = check.similarity_threshold,
        max_assistant_in_row = check.max_assistant_in_row,
    ):
        _skip(context, runtime, chat_id, "local_check")
        return None
    if runtime.cfg.proactive.enable_persona_judge:
        try:
            allowed = parse_judgement(
                await _complete(
                    "checker",
                    secrets,
                    runtime.cfg.llm_config("checker"),
                    f"{card.facts(bot_name)}\n\n{_JUDGE_INSTRUCTION}",
                    f"{material}\n\n她准备发：{draft}",
                )
            )
        except LLMError:
            allowed = False
        if not allowed:
            _skip(context, runtime, chat_id, "judge")
            return None
    log_step(context, runtime, chat_id=chat_id, step="proactive.send", fields={"text": draft})
    receipt = _receipt(
        context,
        runtime,
        state,
        chat_id  = chat_id,
        bot_name = bot_name,
        text     = draft,
        secrets  = secrets,
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
        _skip(context, runtime, "", "outside_active_hours")
        return []
    try:
        slot = routine_slot_now(runtime.cfg.personality.card, moment)
    except ValueError:
        slot = None
    if slot is not None and slot.availability == "asleep":
        _skip(context, runtime, "", "asleep")
        return []
    if slot is not None and slot.availability == "busy" and random.random() < 0.5:
        _skip(context, runtime, "", "busy")
        return []
    life_text = state_text(slot) if runtime.cfg.life.enable_schedule_state else ""
    state     = bound_state(context)
    groups    = _target_groups(context, cfg.group_ids)
    if not groups:
        _skip(context, runtime, "", "no_target_group")
    deliveries: list[ScheduledDelivery] = []
    for group_id in groups:
        delivery = await _consider_group(
            context, runtime, state, group_id, now=now, life_text=life_text
        )
        if delivery is not None:
            deliveries.append(delivery)
    return deliveries
