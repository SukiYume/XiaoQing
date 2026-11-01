"""把对话上下文转换为有界、可执行的 PFC 行动计划。

规划器只接收裁剪后的目标、记忆和行动摘要，并把模型输出收窄到固定动作集合。模型
不可用、超时、JSON 异常或未知动作时，群聊必须退回 wait，私聊才退回 direct_reply。
新鲜轮次里已经通过频控的全群邀请可直接进入回复；其余消息由规划器按话题和时机判断要不要接。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..config.config import PersonalityConfig
from ..llm.gateway import chat_completions_raw_with_fallback_paths, extract_response_content
from ..llm.llm_config import LLMCallConfig
from ..llm.prompt_builder import build_dialogue_prompt
from ..memory.memory import StoredMessage, is_conversation_reply
from ..participation import classify_group_participation_cue as _group_participation_cue
from ..utils.text import clip_text
from .pfc_utils import get_items_from_json

_logger = logging.getLogger("plugin.xiaoqing_chat")

# 问句距离上一位群友的发言在这个时间内，视为在接对方的话。
_EXCHANGE_FOLLOWUP_SECONDS = 90.0

_ALLOWED_PLANNER_ACTIONS = frozenset(
    {
        "block_and_ignore",
        "direct_reply",
        "end_conversation",
        "fetch_knowledge",
        "listening",
        "rethink_goal",
        "send_new_message",
        "wait",
    }
)


def _log_planner_step(fields: dict[str, Any]) -> None:
    """规划不依赖日志可用性；日志边界失败时只跳过本条观测。"""

    try:
        _logger.info(
            "xiaoqing_chat step=%s",
            json.dumps(fields, ensure_ascii=False),
        )
    except Exception:
        return


# 两种规划场景共用的上下文与输出格式；差异只在开头的场景说明和决策指导。
_PLANNER_CONTEXT = """{knowledge_info_str}
【最近行动概要】
{action_history_summary}

【上次行动】
{last_action_context}

【时间提示】
{time_since_last_bot_message_info}{participation_info}{timeout_context}

最近对话（注意：仔细看每条消息的发送者，不同用户是不同的人）：
{chat_history_text}

"""


def _planner_output_format(actions: str, thinking_hint: str) -> str:
    return (
        "可选 action（必须选其一）：\n"
        f"{actions}\n\n"
        "请先写出你的思考过程（thinking），再给出行动决策。输出 JSON：\n"
        "{{\n"
        f'  "thinking": "{thinking_hint}",\n'
        '  "action": "选择的行动类型",\n'
        '  "reason": "选择该行动的原因",\n'
        '  "wait_seconds": 0\n'
        "}}\n"
        "说明：wait_seconds 仅在 action 为 wait 或 listening 时生效，必须是 JSON 整数，"
        "表示等待秒数（0~30），其余 action 填 0。"
    )


PROMPT_INITIAL_REPLY_COMPACT = (
    """{persona_text}。你在QQ{channel}闲聊。

目标：
{goals_str}

【决策指导——请逐条思考】
1. 你是活跃的群友。群里在聊的事你感兴趣、有话可说，就接：一句看法、吐槽、好奇的追问、玩笑或共鸣都可以，选择 direct_reply。要不要开口，看的是话题和你有没有话说
2. 开口不需要亲身经历或专业知识：对别人的经历表示惊讶或羡慕、顺着笑点往前推一步、问一句细节，都是正常的群聊参与；话题碰到你的兴趣、生活或你说过的话时更该开口
3. 不替别人编造或揣测感受和经历，也不替他回答他自己的事；接话只聊你自己的看法、感受和经历
4. 这些情况选择 wait 或 listening：你能说的只是复述或空泛附和（“确实”“哈哈”）；话题已经收尾；你刚接连发过言，该给别人留位置；对方明确拒聊或厌烦
5. 群友正在接连复读同一句时，跟一句队形算接梗，可以 direct_reply
6. 对方追问你、纠正你或延续你刚才的话时，通常适合继续
7. 消息长度不能单独决定行动：短消息也可能有明确交流作用，长消息也可能没什么可接
8. 对不确定的信息先判断是否需要 fetch_knowledge；无需检索时可以承认不确定，但不能编造解释
9. 已经提出而无人回应的问题不要原样追问，按最新话题重新判断是否有必要发言
10. 媒体摘要是消息内容的一部分，应根据它表达的意思和当前上下文判断行动，而不是因为出现媒体就回复
11. 时间提示说你已经好几条没出声时，不要再拿同样的理由继续旁听：有一句自然、有内容的话可接就开口

"""
    + _PLANNER_CONTEXT
    + _planner_output_format(
        "fetch_knowledge / listening / direct_reply / wait / rethink_goal / end_conversation / block_and_ignore",
        "你对当前局势的分析：大家在聊什么、气氛如何；你对哪点感兴趣，接一句会说什么",
    )
)

PROMPT_FOLLOW_UP_COMPACT = (
    """{persona_text}。你在QQ{channel}闲聊，刚刚你已经回复过对方。

目标：
{goals_str}

【决策指导——请逐条思考】
1. 先读懂最新消息在说什么、和你刚才的话有什么关系：接着你的话说的，通常适合继续；聊到别的事上，就看新话题你感不感兴趣、有没有新内容可说
2. 你刚刚已经发过消息，默认给其他人留出轮次；对话需要你继续承接，或者你确实有新的话想说时再发言。群友接连复读同一句时，跟一句队形属于接梗，不算抢话；你已经跟过同一句就不再跟
3. 不替别人编造或揣测感受和经历，也不替他回答他自己的事；接话只聊你自己的看法、感受和经历
4. 消息长短、有没有问号都不能单独决定行动，要综合话题、时机和你有没有新内容
5. 出现拒聊、厌烦或自然收尾时，选择 wait、listening 或 end_conversation
6. 不重复未获回应的问题，也不换一种说法重复刚才的结论、笑点或行动
7. 不确定的信息可以检索、承认不确定或不接，不要根据零散关键词猜测
8. 媒体摘要与文字具有同等地位，根据其交际含义和上下文判断是否继续

"""
    + _PLANNER_CONTEXT
    + _planner_output_format(
        "fetch_knowledge / wait / listening / send_new_message / rethink_goal / end_conversation / block_and_ignore",
        "你对当前局势的分析：大家在聊什么、对你刚才的话什么反应；你还有没有新的话想说",
    )
)

PROMPT_END_DECISION = """{persona_text}。刚刚你决定结束一场 QQ {channel}。

【你们之前的聊天记录】
{chat_history_text}

你觉得你们的对话已经完整结束了吗？有时候，在对话自然结束后再说点什么可能会有点奇怪，但有时也可能需要一条简短的消息来圆满结束。
只有额外一句话能自然补足尚未表达的收尾时才输出 "yes"；不要为了礼貌固定追加告别模板。
如果觉得当前状态下直接结束对话更好，没有必要再发消息，就输出 "no"。

请以 JSON 格式输出你的选择：
{{
    "say_bye": "yes/no",
    "reason": "选择 yes 或 no 的原因和内心想法 (简要说明)"
}}

注意：请严格按照 JSON 格式输出，不要包含任何其他内容。"""


@dataclass(frozen=True)
class PFCPlan:
    action      : str
    reason      : str
    thinking    : str = ""
    wait_seconds: int = 0


def _goals_to_text(goal_list: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in goal_list[-5:]:
        if not isinstance(item, dict):
            continue
        g = str(item.get("goal", "") or "").strip()
        # 目标分析器写入 reasoning；旧持久化记录可能仍是 reason。
        r = str(item.get("reasoning", "") or item.get("reason", "") or "").strip()
        if not g:
            continue
        if r:
            lines.append(f"- {g}（原因：{r}）")
        else:
            lines.append(f"- {g}")
    return "\n".join(lines).strip()


def _knowledge_to_text(knowledge_list: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in knowledge_list[-6:]:
        if not isinstance(item, dict):
            continue
        t = str(item.get("text", "") or "").strip()
        if t:
            lines.append(f"- {t}")
    if not lines:
        return ""
    return "【已知的知识/记忆信息】\n" + "\n".join(lines)


def _time_since_last_bot(history: Sequence[StoredMessage]) -> str:
    now = time.time()
    for msg in reversed(history[-200:]):
        if not is_conversation_reply(msg):
            continue
        diff = max(0.0, now - float(msg.ts or now))
        if diff < 60.0:
            return f"提示：你上一条成功发送的消息是在 {diff:.1f} 秒前。\n"
        return ""
    return ""


# 群友连着说了这么多条、小青一直没出声时，提醒规划器她在这段对话里的参与情况。
_QUIET_STREAK_HINT_MIN = 3


def _participation_hint(history: Sequence[StoredMessage]) -> str:
    """这段对话里小青已经多少条消息没开口。

    规划器只看到眼前几条，每条单独看都“不是在问我”，于是一整段对话都在旁听。告诉它
    已经沉默了多久，它才能像群友一样意识到该说句话了。
    """

    quiet = 0
    for msg in reversed(history):
        if is_conversation_reply(msg):
            if quiet >= _QUIET_STREAK_HINT_MIN:
                return f"提示：你上次开口之后，群友又说了 {quiet} 条，你一直没出声。\n"
            return ""
        if msg.role == "user":
            quiet += 1
    if quiet >= _QUIET_STREAK_HINT_MIN:
        return f"提示：这段对话里你还没开过口，群友已经说了 {quiet} 条。\n"
    return ""


def _follows_another_speaker(history: Sequence[StoredMessage]) -> bool:
    """本轮消息是否紧跟另一位群友的发言；历史末条即本轮消息。"""

    if len(history) < 2:
        return False
    current, previous = history[-1], history[-2]
    if current.role != "user" or previous.role != "user":
        return False
    if current.user_id is None or previous.user_id in (None, current.user_id):
        return False
    if not current.ts or not previous.ts:
        return False
    return 0.0 <= float(current.ts) - float(previous.ts) <= _EXCHANGE_FOLLOWUP_SECONDS


def _parse_plan(content: str, fallback_action: str) -> PFCPlan:
    """解析规划器输出；结构不合法或动作不在白名单时退回保守动作。"""
    ok, obj = get_items_from_json(
        content,
        "action",
        "reason",
        optional_items = ("thinking", "wait_seconds"),
        default_values = {
            "action"      : fallback_action,
            "reason"      : "",
            "thinking"    : "",
            "wait_seconds": 0,
        },
        required_types = {"action": str, "reason": str},
        allow_array    = False,
    )
    if not ok or not isinstance(obj, dict):
        return PFCPlan(action=fallback_action, reason="planner_invalid_response")
    act              = str(obj.get("action", "") or "").strip().casefold()
    reason           = str(obj.get("reason", "") or "").strip()
    raw_thinking     = obj.get("thinking", "")
    thinking         = raw_thinking.strip() if isinstance(raw_thinking, str) else ""
    raw_wait_seconds = obj.get("wait_seconds", 0)
    wait_seconds     = raw_wait_seconds if type(raw_wait_seconds) is int else 0
    wait_seconds     = max(0, min(30, wait_seconds))
    if act not in _ALLOWED_PLANNER_ACTIONS:
        return PFCPlan(action=fallback_action, reason="planner_invalid_action")
    return PFCPlan(action=act, reason=reason, thinking=thinking, wait_seconds=wait_seconds)


async def plan_next_action(
    *,
    secrets: dict[str, Any],
    bot_name: str,
    is_private: bool,
    personality: PersonalityConfig,
    history: Sequence[StoredMessage],
    goal_list: Sequence[dict[str, Any]],
    knowledge_list: Sequence[dict[str, Any]],
    action_history_summary: str,
    last_action_context: str,
    timeout_context: str,
    last_successful_reply_action: str | None,
    llm: LLMCallConfig,
    current_text: str = "",
) -> PFCPlan:
    model           = secrets.get("model", "")
    fallback_action = "direct_reply" if is_private else "wait"
    if "_ai" in secrets and secrets.get("_ai") is None:
        return PFCPlan(action=fallback_action, reason="ai_route_unavailable")

    persona_text       = personality.card.facts(bot_name)
    goals_str          = _goals_to_text(goal_list) or "- 自然聊天"
    knowledge_info_str = _knowledge_to_text(knowledge_list)
    chat_history_text  = build_dialogue_prompt(
        history, bot_name=bot_name, truncate=True, max_chars=1200
    )
    time_info         = _time_since_last_bot(history)
    channel           = "私聊" if is_private else "群聊"
    participation_cue = ""
    if not is_private and not last_successful_reply_action:
        # 分类器已经排除点给其他人的消息；上游频控也已经完成概率、间隔和限流。
        # 新鲜轮次里的明确全群邀请直接回复，避免规划器仅凭“没点名”再次否决。
        participation_cue = _group_participation_cue(current_text)
        if participation_cue == "open_question" and _follows_another_speaker(history):
            # 紧接着别人说的问句多半是在接对方的话（“藤子 ban 吗？”），谁问谁交给规划器判断。
            participation_cue = ""
        if participation_cue:
            return PFCPlan(
                action = "direct_reply",
                reason = f"fresh_group_participation:{participation_cue}",
            )

    tpl = (
        PROMPT_FOLLOW_UP_COMPACT
        if last_successful_reply_action in ("direct_reply", "send_new_message")
        else PROMPT_INITIAL_REPLY_COMPACT
    )

    prompt = tpl.format(
        persona_text       = persona_text,
        channel            = channel,
        goals_str          = goals_str,
        knowledge_info_str = (clip_text(knowledge_info_str, 480) + "\n")
        if knowledge_info_str
        else "",
        action_history_summary           = clip_text(action_history_summary, 360) or "（暂无）",
        last_action_context              = clip_text(last_action_context, 360) or "（暂无）",
        time_since_last_bot_message_info = time_info,
        participation_info               = "" if is_private else _participation_hint(history),
        timeout_context                  = timeout_context.strip(),
        chat_history_text                = chat_history_text,
    )
    if current_text.strip():
        prompt += "\n本轮目标消息：\n" + current_text.strip()

    try:
        t0 = time.monotonic()
        resp, _path = await chat_completions_raw_with_fallback_paths(
            route    = "reasoning",
            secrets  = secrets,
            messages = [{"role": "user", "content": prompt}],
            **llm.as_kwargs(),
        )
        _log_planner_step(
            {
                "step"        : "pfc.planner.ok",
                "elapsed_s"   : round(time.monotonic() - t0, 3),
                "model"       : model,
                "endpoint"    : _path,
                "prompt_chars": len(prompt),
            }
        )
    except TimeoutError:
        _log_planner_step(
            {
                "step"        : "pfc.planner.timeout",
                "timeout_s"   : float(llm.timeout_seconds),
                "model"       : model,
                "prompt_chars": len(prompt),
            }
        )
        return PFCPlan(action=fallback_action, reason="planner_timeout")
    except Exception as exc:
        _log_planner_step(
            {
                "step"      : "pfc.planner.error",
                "error_type": type(exc).__name__,
                "timeout_s" : float(llm.timeout_seconds),
                "model"     : model,
            }
        )
        return PFCPlan(action=fallback_action, reason="planner_failed")
    return _parse_plan(extract_response_content(resp), fallback_action)


async def decide_say_bye(
    *,
    secrets: dict[str, Any],
    bot_name: str,
    is_private: bool,
    personality: PersonalityConfig,
    history: Sequence[StoredMessage],
    llm: LLMCallConfig,
) -> tuple[bool, str]:
    if "_ai" in secrets and secrets.get("_ai") is None:
        return False, ""

    persona_text      = personality.card.facts(bot_name)
    channel           = "私聊" if is_private else "群聊"
    chat_history_text = build_dialogue_prompt(
        history, bot_name=bot_name, truncate=True, max_chars=2200
    )
    prompt = PROMPT_END_DECISION.format(
        persona_text=persona_text, channel=channel, chat_history_text=chat_history_text
    )

    try:
        resp, _ = await chat_completions_raw_with_fallback_paths(
            route    = "reasoning",
            secrets  = secrets,
            messages = [{"role": "user", "content": prompt}],
            **llm.as_kwargs(),
        )
    except Exception:
        return False, ""
    content = extract_response_content(resp)
    ok, obj = get_items_from_json(
        str(content),
        "say_bye",
        "reason",
        default_values = {"say_bye": "no", "reason": ""},
        required_types = {"say_bye": str, "reason": str},
        allow_array    = False,
    )
    if not ok or not isinstance(obj, dict):
        return False, ""
    val = str(obj.get("say_bye", "") or "").strip().lower()
    return val == "yes", str(obj.get("reason", "") or "").strip()
