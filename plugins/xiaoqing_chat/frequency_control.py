"""依据上下文、频率和活跃度控制群聊中的主动回复概率。"""

from __future__ import annotations

import dataclasses
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .participation import is_low_information_group_turn

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
    # 作息对插话概率的乘数：忙或睡时小于 1。
    routine_factor: float = 1.0

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
        }
        if self.probability is not None:
            fields["probability"] = round(self.probability, 4)
        if self.roll is not None:
            fields["roll"] = round(self.roll, 4)
        if self.heartflow_score is not None:
            fields["heartflow_score"] = round(self.heartflow_score, 4)
        if self.routine_factor != 1.0:
            fields["routine_factor"] = round(self.routine_factor, 4)
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


def _min_reply_interval(cfg, *, active_topic: bool) -> float:
    interval = float(cfg.min_reply_interval_seconds)
    if active_topic:
        # 群聊中连续话题允许更快回复；别人紧接着追问时不再套用普通群聊的长间隔。
        interval = min(interval, max(0.0, cfg.active_topic_min_reply_interval))
    return interval


def _hard_limit_reason(
    cfg, decision: ReplyGateDecision, text: str, *, is_private: bool, follow_up: bool = False
) -> str:
    """限流、冷却和明显不该接的消息无论其他信号如何都拦下。

    ``follow_up``：这句紧跟着她的发言、多半在接她的话时不算每分钟上限。她刚问了“推番还是推游戏”，
    对方答“番”，不能因为这一分钟说得多就不理；最小间隔和连续冷却照旧。
    """

    if decision.min_interval_seconds > 0 and (
        decision.seconds_since_last_reply < decision.min_interval_seconds
    ):
        return "min_interval"
    if (
        not follow_up
        and cfg.max_replies_per_minute > 0
        and decision.replies_last_minute >= cfg.max_replies_per_minute
    ):
        return "max_replies_per_minute"
    if decision.cooldown_left_seconds > 0:
        return "continuous_cooldown"
    if not is_private and is_low_information_group_turn(text):
        return "low_information"
    # 点名或 @ 了别的群友不在这里拦：话题她感兴趣也可以接一句，由规划器判断。
    return ""


def _participation_probability(cfg, *, active_topic: bool) -> float:
    """普通插话概率，活跃话题按配置提高概率。"""

    probability = float(cfg.reply_probability_base)
    if active_topic:
        probability = max(probability, min(1.0, cfg.active_topic_reply_probability))
    return probability


async def _heartflow_score(cfg, state, chat_id: str, text: str, since: float) -> float:
    heartflow = cfg.heartflow
    score     = await state.heartflow.score_async(
        chat_id                  = chat_id,
        text                     = text,
        seconds_since_last_reply = since,
        base                     = heartflow.base_score,
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


# 群里接连这么多条她都没出声时，作息的压制减到一半。
_ROUTINE_RELIEF_STREAK = 6


def _routine_factor_with_streak(factor: float, no_reply_streak: int) -> float:
    """上课或躺在床上时，群里一直有人说话、她一直没出声，手机响多了总会看一眼。

    作息的压制随连续没回复的条数逐步减弱，最多减到一半；接不接仍由规划器看话题决定。
    """

    relief = min(max(no_reply_streak, 0), _ROUTINE_RELIEF_STREAK) / (2 * _ROUTINE_RELIEF_STREAK)
    return factor + (1.0 - factor) * relief


async def decide_reply(
    runtime: ChatRuntime,
    state,
    chat_id: str,
    text: str,
    is_private: bool,
    enable_private_brain_chat: bool,
    *,
    routine_factor: float   = 1.0,
    follows_own_reply: bool = False,
) -> bool:
    """判断普通消息是否主动参与，并把决策细节记入运行状态供日志使用。

    明确冲机器人来的消息已经由 attention_gate 在 handler 层强制回复；这里只处理硬频控、
    普通插话概率、heartflow 软加分和静默期补偿。``routine_factor`` 是作息给的乘数，
    最后作用在概率上：角色在上课或睡觉时更少插话，群里一直热闹而她没出声时压得轻一些。

    ``follows_own_reply``：这句话紧跟在她刚发的消息后面，多半是在接她的话（损她一句、
    给她起外号，不一定带名字）。不掷骰子，过了硬频控就交给规划器看该不该接。
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

    decision = ReplyGateDecision(
        should_reply             = False,
        reason                   = "",
        seconds_since_last_reply = since,
        min_interval_seconds     = _min_reply_interval(
            cfg, active_topic=active_topic or follows_own_reply
        ),
        replies_last_minute    = len(window),
        max_replies_per_minute = cfg.max_replies_per_minute,
        cooldown_left_seconds  = max(0.0, state.get_continuous_cooldown_until(chat_id) - now),
        active_topic           = active_topic,
    )
    if reason := _hard_limit_reason(
        cfg, decision, text, is_private=is_private, follow_up=follows_own_reply
    ):
        decision = dataclasses.replace(decision, reason=reason)
    elif enable_private_brain_chat and is_private:
        # 深度对话模式下跳过概率控制。
        decision = dataclasses.replace(decision, should_reply=True, reason="private_brain_chat")
    elif follows_own_reply and not is_private:
        decision = dataclasses.replace(decision, should_reply=True, reason="follows_own_reply")
    else:
        probability = _participation_probability(cfg, active_topic=active_topic)
        score = None
        if cfg.heartflow.enable_heartflow:
            # Heartflow 信号作为软加分，不作为硬门槛。
            score       = await _heartflow_score(cfg, state, chat_id, text, since)
            probability = probability + (1.0 - probability) * max(
                0.0, score - cfg.heartflow.base_score
            )
        streak      = (await state.heartflow.get_async(chat_id)).no_reply_streak
        probability = max(0.0, min(1.0, _streak_adjusted(probability, streak)))
        factor      = (
            1.0
            if is_private
            else _routine_factor_with_streak(max(0.0, min(1.0, routine_factor)), streak)
        )
        probability = probability * factor
        roll        = random.random()
        decision    = dataclasses.replace(
            decision,
            should_reply    = roll < probability,
            reason          = "allowed" if roll < probability else "probability",
            probability     = probability,
            roll            = roll,
            no_reply_streak = streak,
            heartflow_score = score,
            routine_factor  = factor,
        )
    state.set_reply_gate_decision(chat_id, decision)
    return decision.should_reply
