"""依据上下文、频率和活跃度控制群聊中的主动回复概率。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import is_question
from .participation import (
    classify_group_participation_cue,
    is_group_turn_directed_to_other,
    is_low_information_group_turn,
)

if TYPE_CHECKING:
    from .runtime_state import _ChatRuntime


@dataclass(frozen=True)
class ReplyGateDecision:
    should_reply: bool
    reason: str
    probability: float | None       = None
    roll: float | None              = None
    seconds_since_last_reply: float = 0.0
    min_interval_seconds: float     = 0.0
    replies_last_minute: int        = 0
    max_replies_per_minute: int     = 0
    cooldown_left_seconds: float    = 0.0
    no_reply_streak: int            = 0
    heartflow_score: float | None   = None
    active_topic: bool              = False
    participation_cue: str          = ""

    def as_log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "reason": self.reason,
            "seconds_since_last_reply": round(self.seconds_since_last_reply, 3),
            "min_interval_seconds": round(self.min_interval_seconds, 3),
            "replies_last_minute": self.replies_last_minute,
            "max_replies_per_minute": self.max_replies_per_minute,
            "cooldown_left_seconds": round(self.cooldown_left_seconds, 3),
            "no_reply_streak": self.no_reply_streak,
            "active_topic": self.active_topic,
            "participation_cue": self.participation_cue,
        }
        if self.probability is not None:
            fields["probability"] = round(self.probability, 4)
        if self.roll is not None:
            fields["roll"] = round(self.roll, 4)
        if self.heartflow_score is not None:
            fields["heartflow_score"] = round(self.heartflow_score, 4)
        return fields


def _freq_record(chat_id: str, runtime: _ChatRuntime, state, *, forced: bool) -> None:
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


def _remember_reply_gate_decision(state, chat_id: str, decision: ReplyGateDecision) -> None:
    has_real_method   = hasattr(type(state), "set_reply_gate_decision")
    has_test_override = "set_reply_gate_decision" in getattr(state, "__dict__", {})
    if not (has_real_method or has_test_override):
        return
    setter = getattr(state, "set_reply_gate_decision", None)
    if not callable(setter):
        return
    try:
        setter(chat_id, decision)
    except Exception:
        return


async def _should_reply_decision(
    runtime: _ChatRuntime,
    state,
    chat_id: str,
    text: str,
    is_private: bool,
    enable_private_brain_chat: bool,
) -> ReplyGateDecision:
    """
    判断普通群聊消息是否应该主动参与。

    明确冲机器人来的消息已经由 attention_gate 在 handler 层强制回复。
    这里只保留硬频控、普通插话概率、heartflow 软加权和静默期补偿。

    Args:
        runtime: 运行时配置
        state: 全局状态
        chat_id: 聊天ID
        text: 消息文本
        is_private: 是否私聊
        enable_private_brain_chat: 是否启用私聊深度对话

    Returns:
        是否应该回复
    """
    now            = time.time()
    last           = state.get_last_reply_ts(chat_id)
    cooldown_until = state.get_continuous_cooldown_until(chat_id)
    cooldown_left  = max(0.0, cooldown_until - now)
    window         = [t for t in state.get_reply_timestamps(chat_id) if now - t < 60.0]
    state.set_reply_timestamps(chat_id, window)
    goal_state = await state.goal_store.get_async(chat_id) if runtime.cfg.goal.enable_goal else None
    goal       = getattr(goal_state, "goal", "") if goal_state else ""
    goal_ts    = float(getattr(goal_state, "ts", 0.0) or 0.0) if goal_state else 0.0

    # --- 硬约束：无论其他信号如何，限流和冷却都必须生效。---
    seconds_since = max(0.0, now - last) if last else 9999.0

    is_active_topic = False
    if not is_private and goal and (now - goal_ts < 300) and (seconds_since < 300):
        is_active_topic = True
    active_followup_question = is_active_topic and is_question(text)
    participation_cue        = "" if is_private else classify_group_participation_cue(text)

    actual_min_interval = runtime.cfg.min_reply_interval_seconds
    if is_active_topic:
        # 群聊中连续话题，允许更快回复
        active_topic_min_interval = max(
            0.0,
            runtime.cfg.active_topic_min_reply_interval,
        )
        actual_min_interval = min(actual_min_interval, active_topic_min_interval)
        if active_followup_question:
            # 别人紧接着追问上一条回复时，不应仍套用普通群聊的长间隔。
            question_min_interval = max(
                0.0,
                runtime.cfg.active_topic_question_min_reply_interval,
            )
            actual_min_interval = min(actual_min_interval, question_min_interval)

    def make_decision(
        should_reply: bool,
        reason: str,
        *,
        probability: float | None     = None,
        roll: float | None            = None,
        no_reply_streak: int          = 0,
        heartflow_score: float | None = None,
    ) -> ReplyGateDecision:
        decision = ReplyGateDecision(
            should_reply             = should_reply,
            reason                   = reason,
            probability              = probability,
            roll                     = roll,
            seconds_since_last_reply = seconds_since,
            min_interval_seconds     = actual_min_interval,
            replies_last_minute      = len(window),
            max_replies_per_minute   = runtime.cfg.max_replies_per_minute,
            cooldown_left_seconds    = cooldown_left,
            no_reply_streak          = no_reply_streak,
            heartflow_score          = heartflow_score,
            active_topic             = is_active_topic,
            participation_cue        = participation_cue,
        )
        _remember_reply_gate_decision(state, chat_id, decision)
        return decision

    if actual_min_interval > 0 and seconds_since < actual_min_interval:
        return make_decision(False, "min_interval")
    if runtime.cfg.max_replies_per_minute > 0 and len(window) >= runtime.cfg.max_replies_per_minute:
        return make_decision(False, "max_replies_per_minute")
    if cooldown_left > 0:
        return make_decision(False, "continuous_cooldown")
    if not is_private and is_low_information_group_turn(text):
        return make_decision(False, "low_information")
    if not is_private and is_group_turn_directed_to_other(text):
        return make_decision(False, "directed_to_other")

    # --- 深度对话模式下跳过概率控制 ---
    if enable_private_brain_chat and is_private:
        return make_decision(True, "private_brain_chat")

    # --- 单层概率控制：这里只处理普通参与，明确私聊/点名已在前面放行 ---
    p = runtime.cfg.reply_probability_base

    if participation_cue:
        # 明确面向全群的问题、邀请和开场梗值得更稳定地进入 PFC；这里仍只提高
        # 概率，前面的间隔、每分钟上限和连续回复冷却继续作为硬约束。
        cue_probability = runtime.cfg.participation_cue_reply_probability
        p               = max(p, min(1.0, cue_probability))

    if (not is_private) and is_active_topic:
        # 使用明确上限而不是按剩余概率大幅增益，避免 0.72 被抬到 0.888 一类过强参与率。
        active_probability = runtime.cfg.active_topic_reply_probability
        p                  = max(p, min(1.0, active_probability))
        if active_followup_question:
            # 活跃话题里的明确追问更像连续对话，而不是随机插话；仍保留小概率静默，
            # 后续 PFC 也会再次判断消息是否真的在跟小青说话。
            question_probability = runtime.cfg.active_topic_question_reply_probability
            p                    = max(p, min(1.0, question_probability))

    # Heartflow 信号作为概率调整（软加分），不再作为硬门槛
    hf_score: float | None = None
    if runtime.cfg.heartflow.enable_heartflow:
        hf_score = await state.heartflow.score_async(
            chat_id                  = chat_id,
            text                     = text,
            goal                     = goal,
            seconds_since_last_reply = seconds_since,
            base                     = runtime.cfg.heartflow.base_score,
            weight_question          = runtime.cfg.heartflow.weight_question,
            weight_goal_match        = runtime.cfg.heartflow.weight_goal_match,
            weight_short_text        = runtime.cfg.heartflow.weight_short_text,
            weight_no_reply_streak   = runtime.cfg.heartflow.weight_no_reply_streak,
            weight_long_silence      = runtime.cfg.heartflow.weight_long_silence,
        )
        hf_bonus = max(0.0, hf_score - runtime.cfg.heartflow.base_score)
        p        = p + (1.0 - p) * hf_bonus

    # --- 动态阈值：连续不回复会逐步降低门槛（参考 MaiBot）。---
    no_reply_streak = (await state.heartflow.get_async(chat_id)).no_reply_streak
    if no_reply_streak >= 5:
        p = min(p * 1.4, 0.95)
    elif no_reply_streak >= 3:
        p = min(p * 1.2, 0.90)

    p = max(0.0, min(1.0, p))

    roll = random.random()
    if roll >= p:
        return make_decision(
            False,
            "probability",
            probability     = p,
            roll            = roll,
            no_reply_streak = no_reply_streak,
            heartflow_score = hf_score,
        )
    return make_decision(
        True,
        "allowed",
        probability     = p,
        roll            = roll,
        no_reply_streak = no_reply_streak,
        heartflow_score = hf_score,
    )


async def _should_reply(
    runtime: _ChatRuntime,
    state,
    chat_id: str,
    text: str,
    is_private: bool,
    enable_private_brain_chat: bool,
) -> bool:
    decision = await _should_reply_decision(
        runtime,
        state,
        chat_id,
        text,
        is_private,
        enable_private_brain_chat,
    )
    return decision.should_reply
