"""依据上下文、频率和活跃度控制群聊中的主动回复概率。"""

from __future__ import annotations

import dataclasses
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import is_question
from .participation import (
    classify_group_participation_cue,
    is_group_repeat_chain,
    is_group_turn_directed_to_other,
    is_low_information_group_turn,
)

if TYPE_CHECKING:
    from .runtime_state import ChatRuntime


@dataclass(frozen=True)
class ReplyGateDecision:
    should_reply            : bool
    reason                  : str
    probability             : float | None = None
    roll                    : float | None = None
    seconds_since_last_reply: float        = 0.0
    min_interval_seconds    : float        = 0.0
    replies_last_minute     : int          = 0
    max_replies_per_minute  : int          = 0
    cooldown_left_seconds   : float        = 0.0
    no_reply_streak         : int          = 0
    heartflow_score         : float | None = None
    active_topic            : bool         = False
    participation_cue       : str          = ""

    def as_log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "reason"                  : self.reason,
            "seconds_since_last_reply": round(self.seconds_since_last_reply, 3),
            "min_interval_seconds"    : round(self.min_interval_seconds, 3),
            "replies_last_minute"     : self.replies_last_minute,
            "max_replies_per_minute"  : self.max_replies_per_minute,
            "cooldown_left_seconds"   : round(self.cooldown_left_seconds, 3),
            "no_reply_streak"         : self.no_reply_streak,
            "active_topic"            : self.active_topic,
            "participation_cue"       : self.participation_cue,
        }
        if self.probability is not None:
            fields["probability"] = round(self.probability, 4)
        if self.roll is not None:
            fields["roll"] = round(self.roll, 4)
        if self.heartflow_score is not None:
            fields["heartflow_score"] = round(self.heartflow_score, 4)
        return fields


def record_reply_frequency(chat_id: str, runtime: ChatRuntime, state, *, forced: bool) -> None:
    now = time.time()
    state.set_last_reply_ts(chat_id, now)
    timestamps = state.get_reply_timestamps(chat_id)
    timestamps.append(now)
    state.set_reply_timestamps(chat_id, timestamps)

    if forced:
        state.set_continuous_reply_count(chat_id, 0)
        return

    c = state.get_continuous_reply_count(chat_id) + 1
    state.set_continuous_reply_count(chat_id, c)
    if runtime.cfg.continuous_reply_limit > 0 and c >= runtime.cfg.continuous_reply_limit:
        state.set_continuous_cooldown_until(
            chat_id, now + max(0.0, runtime.cfg.continuous_cooldown_seconds)
        )
        state.set_continuous_reply_count(chat_id, 0)


async def _repeat_chain_cue(state, chat_id: str, text: str) -> str:
    """当前消息若在跟前一位群友复读，就作为参与信号；历史不可读时不影响原判断。"""

    try:
        recent = await state.memory_store.get_recent_async(chat_id, max_items=4)
    except Exception:
        return ""
    user_texts = [message.content for message in recent if message.role == "user"]
    # 当前消息通常已先写入历史；去掉末尾的自己再与前一位比较。
    if user_texts and user_texts[-1].strip() == str(text or "").strip():
        user_texts = user_texts[:-1]
    return "repeat_chain" if is_group_repeat_chain(text, user_texts) else ""


def _min_reply_interval(cfg, *, active_topic: bool, followup_question: bool) -> float:
    interval = float(cfg.min_reply_interval_seconds)
    if active_topic:
        # 群聊中连续话题允许更快回复；别人紧接着追问时不再套用普通群聊的长间隔。
        interval = min(interval, max(0.0, cfg.active_topic_min_reply_interval))
        if followup_question:
            interval = min(interval, max(0.0, cfg.active_topic_question_min_reply_interval))
    return interval


def _hard_limit_reason(cfg, decision: ReplyGateDecision, text: str, *, is_private: bool) -> str:
    """限流、冷却和明显不该接的消息无论其他信号如何都拦下。"""

    if decision.min_interval_seconds > 0 and (
        decision.seconds_since_last_reply < decision.min_interval_seconds
    ):
        return "min_interval"
    if (
        cfg.max_replies_per_minute > 0
        and decision.replies_last_minute >= cfg.max_replies_per_minute
    ):
        return "max_replies_per_minute"
    if decision.cooldown_left_seconds > 0:
        return "continuous_cooldown"
    if not is_private and is_low_information_group_turn(text):
        return "low_information"
    if not is_private and is_group_turn_directed_to_other(text):
        return "directed_to_other"
    return ""


def _participation_probability(
    cfg, *, cue: str, active_topic: bool, followup_question: bool
) -> float:
    """普通插话概率；面向全群的线索和活跃话题只抬高到各自的上限，不叠加放大。"""

    probability = float(cfg.reply_probability_base)
    if cue:
        probability = max(probability, min(1.0, cfg.participation_cue_reply_probability))
    if active_topic:
        probability = max(probability, min(1.0, cfg.active_topic_reply_probability))
        if followup_question:
            # 活跃话题里的明确追问更像连续对话；仍保留小概率静默，PFC 会再判断是否在跟机器人说话。
            probability = max(probability, min(1.0, cfg.active_topic_question_reply_probability))
    return probability


async def _heartflow_score(cfg, state, chat_id: str, text: str, goal: str, since: float) -> float:
    heartflow = cfg.heartflow
    score     = await state.heartflow.score_async(
        chat_id                  = chat_id,
        text                     = text,
        goal                     = goal,
        seconds_since_last_reply = since,
        base                     = heartflow.base_score,
        weight_question          = heartflow.weight_question,
        weight_goal_match        = heartflow.weight_goal_match,
        weight_short_text        = heartflow.weight_short_text,
        weight_no_reply_streak   = heartflow.weight_no_reply_streak,
        weight_long_silence      = heartflow.weight_long_silence,
    )
    return float(score)


def _streak_adjusted(probability: float, no_reply_streak: int) -> float:
    """连续不回复会逐步降低门槛。"""
    if no_reply_streak >= 5:
        return min(probability * 1.4, 0.95)
    if no_reply_streak >= 3:
        return min(probability * 1.2, 0.90)
    return probability


async def decide_reply(
    runtime: ChatRuntime,
    state,
    chat_id: str,
    text: str,
    is_private: bool,
    enable_private_brain_chat: bool,
) -> bool:
    """判断普通消息是否主动参与，并把决策细节记入运行状态供日志使用。

    明确冲机器人来的消息已经由 attention_gate 在 handler 层强制回复；这里只处理硬频控、
    普通插话概率、heartflow 软加分和静默期补偿。
    """

    cfg    = runtime.cfg
    now    = time.time()
    last   = state.get_last_reply_ts(chat_id)
    since  = max(0.0, now - last) if last else 9999.0
    window = [t for t in state.get_reply_timestamps(chat_id) if now - t < 60.0]
    state.set_reply_timestamps(chat_id, window)
    goal_state   = await state.goal_store.get_async(chat_id) if cfg.goal.enable_goal else None
    goal         = goal_state.goal if goal_state else ""
    active_topic = bool(
        not is_private
        and goal_state is not None
        and goal
        and now - float(goal_state.ts) < 300
        and since < 300
    )
    followup_question = active_topic and is_question(text)
    cue               = ""
    if not is_private:
        cue = classify_group_participation_cue(text) or await _repeat_chain_cue(
            state, chat_id, text
        )

    decision = ReplyGateDecision(
        should_reply             = False,
        reason                   = "",
        seconds_since_last_reply = since,
        min_interval_seconds     = _min_reply_interval(
            cfg, active_topic=active_topic, followup_question=followup_question
        ),
        replies_last_minute    = len(window),
        max_replies_per_minute = cfg.max_replies_per_minute,
        cooldown_left_seconds  = max(0.0, state.get_continuous_cooldown_until(chat_id) - now),
        active_topic           = active_topic,
        participation_cue      = cue,
    )
    if reason := _hard_limit_reason(cfg, decision, text, is_private=is_private):
        decision = dataclasses.replace(decision, reason=reason)
    elif enable_private_brain_chat and is_private:
        # 深度对话模式下跳过概率控制。
        decision = dataclasses.replace(decision, should_reply=True, reason="private_brain_chat")
    else:
        probability = _participation_probability(
            cfg, cue=cue, active_topic=active_topic, followup_question=followup_question
        )
        score = None
        if cfg.heartflow.enable_heartflow:
            # Heartflow 信号作为软加分，不作为硬门槛。
            score       = await _heartflow_score(cfg, state, chat_id, text, goal, since)
            probability = probability + (1.0 - probability) * max(
                0.0, score - cfg.heartflow.base_score
            )
        streak      = (await state.heartflow.get_async(chat_id)).no_reply_streak
        probability = max(0.0, min(1.0, _streak_adjusted(probability, streak)))
        roll        = random.random()
        decision    = dataclasses.replace(
            decision,
            should_reply    = roll < probability,
            reason          = "allowed" if roll < probability else "probability",
            probability     = probability,
            roll            = roll,
            no_reply_streak = streak,
            heartflow_score = score,
        )
    state.set_reply_gate_decision(chat_id, decision)
    return decision.should_reply
