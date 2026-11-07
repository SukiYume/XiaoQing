"""私聊主动联系：白名单里的朋友，真实的发现或到期的事才开口。默认关闭，白名单为空时不运行。

材料有三种：① 她最近刷到过、和这位朋友资料里的兴趣对得上的事（来自 xiaoqing_life 的分享候选）；
② 这位朋友到期的未完话头（比如“明天考试”，第二天顺口问一句）；③ 聊过的朋友好几天没说话了
（``private_miss_after_days``），想起对方随口说一句。都没有就不发。

联系协调（每条都单独检查）：每天次数上限、两次之间的间隔、对方最近刚说过话就不打扰、她上一条
主动发的还没被回就不再发、同一件经历只分享给同一个人一次。睡觉、忙碌与活跃时段由调用方检查。
起草、本地复读检查、人设自检与投递回执复用群聊主动开口的流程；节奏记录放在 ``private:<user_id>``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.delivery import ScheduledDelivery
from core.plugin_base import segments

from ..chat_runtime import ai_route_context, configured_bot_name
from ..llm.prompt_builder import build_dialogue_prompt
from ..logging_utils import log_step
from ..memory.person_profile import load_profile
from ..persona import SCOPE_CHAT
from ..utils.wall_clock import describe_now, local_datetime
from .online import fetch_share_candidates
from .open_loops import open_loop_block
from .proactive import (
    _DIALOGUE_ITEMS,
    _receipt,
    _skip,
    draft_through_judge,
    parse_life_ref,
    share_block,
)
from .self_timeline import self_timeline_block

_PRIVATE_DRAFT_INSTRUCTION = """这是一次主动找朋友说话的机会，不是对方在跟你说话。你们是私聊。
材料里是你最近真的刷到、也许对方会感兴趣的事，对方之前提过、时间已经过了的事，或者你们好几天没聊了。像想到对方时随手发的一两句就好，没人回也成立。
事实按材料说；你自己的近况可以是模糊的小事，但要和“你最近说过的关于自己的事”对得上。
没有自然的由头，或者和最近的聊天重复，就选择 skip；拿不准也 skip。
只输出一个 JSON 对象：{"decision":"send","text":"要发的话","life_ref":材料里分享的那件事的编号（不是分享就省略）} 或 {"decision":"skip","reason":"no_natural_opening"}"""

_PRIVATE_JUDGE_INSTRUCTION = """现在不是让你写消息，而是替这个角色做一次发送前自检。上面是她的人设。
下面这句话是她准备主动私聊发给一位朋友的。判断能不能发：它像不像她本人随手发给朋友的话，和最近的聊天接不接得上，说的事是否都在材料里，和她最近说过的关于自己的事是否一致。
你只能放行或拦下，不能改写。只输出 JSON：{"decision":"send","reason":"一句短理由"} 或 {"decision":"drop","reason":"一句短理由"}"""

_MIN_TOPIC_CHARS = 2


def profile_text(context: Any, chat_id: str, user_id: int) -> str:
    """这位朋友在私聊里的人物资料（事实列表拼成一段）；读不到就是空。"""

    try:
        profile = load_profile(Path(context.data_dir), chat_id=chat_id, subject_id=user_id)
    except Exception:
        return ""
    return " ".join(profile.facts) if profile is not None else ""


def matches_interests(candidate: dict[str, Any], facts: str) -> bool:
    """候选的话题（或标题里连续两个字）出现在朋友资料里才算对得上兴趣。"""

    if not facts:
        return False
    for topic in candidate.get("topics", []):
        if len(topic) >= _MIN_TOPIC_CHARS and topic in facts:
            return True
    return False


def days_since_last_chat(history: list[Any], *, now: float) -> float:
    """两人上一次说话到现在过了几天；还没和对方聊过时是 0（不因想念去找陌生人）。"""

    if not any(m.role == "user" for m in history):
        return 0.0
    last = max(float(m.ts or 0.0) for m in history)
    return max(0.0, now - last) / 86400.0 if last > 0 else 0.0


def miss_block(days: float) -> str:
    return (
        f"你们已经 {int(days)} 天没聊了。想起对方时可以随口发一句：说说你自己此刻的小事，"
        "带一点想念或小委屈也行；不追问对方为什么没找你，不要求回复，不发“在吗”这种签到。"
    )


def friend_spoke_recently(history: list[Any], *, now: float, quiet_minutes: float) -> bool:
    return any(
        m.role == "user" and 0 <= now - float(m.ts or 0.0) < quiet_minutes * 60.0 for m in history
    )


def unanswered(history: list[Any], last_ts: float) -> bool:
    """她上一条主动私聊之后对方还没说过话。"""

    return last_ts > 0 and not any(
        m.role == "user" and float(m.ts or 0.0) > last_ts for m in history
    )


async def consider_private(
    context: Any, runtime: Any, state: Any, user_id: int, *, now: float, life_text: str
) -> ScheduledDelivery | None:
    cfg     = runtime.cfg.proactive
    chat_id = f"u{user_id}"
    key     = f"private:{user_id}"
    record  = state.proactive_state.get(key)
    today   = local_datetime(now).strftime("%Y-%m-%d")
    if record.count_on(today) >= cfg.private_max_per_day:
        _skip(context, runtime, key, "private_daily_cap")
        return None
    if now - record.last_ts < cfg.private_min_interval_hours * 3600.0:
        _skip(context, runtime, key, "private_interval", minutes_since=(now - record.last_ts) / 60)
        return None
    if now - record.last_attempt_ts < cfg.retry_after_skip_minutes * 60.0:
        _skip(context, runtime, key, "private_recent_attempt")
        return None
    history = list(await state.memory_store.get_recent_async(chat_id, max_items=40))
    if friend_spoke_recently(history, now=now, quiet_minutes=cfg.private_quiet_minutes):
        _skip(context, runtime, key, "private_friend_active")
        return None
    if unanswered(history, record.last_ts):
        _skip(context, runtime, key, "private_unanswered")
        return None

    life  = runtime.cfg.life
    facts = profile_text(context, chat_id, user_id)
    offered = await fetch_share_candidates(context, runtime, exclude_ids=record.shared_life_ids)
    candidates = [c for c in offered if matches_interests(c, facts)]
    loop       = None
    if life.enable_open_loops:
        found = state.open_loops.for_speaker(
            chat_id,
            user_id,
            followup_seconds = float(life.open_loop_followup_hours) * 3600.0,
            now              = now,
        )
        if isinstance(found, tuple) and found[1] == "ask":
            loop = found[0]
    quiet_days = days_since_last_chat(history, now=now)
    missing = cfg.private_miss_after_days > 0 and quiet_days >= cfg.private_miss_after_days
    if not candidates and loop is None and not missing:
        _skip(context, runtime, key, "private_no_material")
        return None

    bot_name = configured_bot_name(context)
    card     = runtime.cfg.personality.card
    persona  = "\n\n".join(
        part for part in (card.facts(bot_name), card.scoped(SCOPE_CHAT, bot_name)) if part
    )
    blocks = [life_text] if life_text else []
    if life.enable_self_timeline:
        entries = state.self_timeline.visible(chat_id, limit=int(life.self_timeline_max_injected))
        if block := self_timeline_block(entries, now=now):
            blocks.append(block)
    parts = [f"现在：{describe_now(now)}。", *blocks]
    if candidates:
        parts.append(share_block([c["line"] for c in candidates]))
    if loop is not None:
        parts.append(open_loop_block(loop, "ask"))
    if missing:
        parts.append(miss_block(quiet_days))
    dialogue = build_dialogue_prompt(history[-_DIALOGUE_ITEMS:], bot_name=bot_name, truncate=True)
    parts.append("你们最近的私聊：\n" + (dialogue or "（还没聊过）"))
    material = "\n\n".join(parts)

    secrets = ai_route_context(context, chat_id=chat_id)
    if secrets.get("_ai") is None:
        _skip(context, runtime, key, "ai_unavailable")
        return None
    await asyncio.to_thread(state.proactive_state.record_attempt, key, ts=now)
    done = await draft_through_judge(
        context,
        runtime,
        chat_id      = key,
        secrets      = secrets,
        system       = f"{persona}\n\n{_PRIVATE_DRAFT_INSTRUCTION}",
        material     = material,
        history      = history,
        judge_system = f"{card.facts(bot_name)}\n\n{_PRIVATE_JUDGE_INSTRUCTION}",
    )
    if done is None:
        return None
    draft, raw_draft = done
    log_step(context, runtime, chat_id=key, step="proactive.send", fields={"text": draft})
    receipt = _receipt(
        context,
        runtime,
        state,
        chat_id         = chat_id,
        bot_name        = bot_name,
        text            = draft,
        secrets         = secrets,
        state_key       = key,
        shared_life_ids = parse_life_ref(raw_draft, candidates),
        reasoning       = "private_contact",
        on_commit=(lambda: state.open_loops.mark_asked(chat_id, loop.loop_id, now=now))
        if loop
        else None,
    )
    return ScheduledDelivery.private(user_id, segments(draft), receipt=receipt)
