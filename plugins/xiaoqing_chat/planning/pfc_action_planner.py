"""把对话上下文转换为有界、可执行的 PFC 行动计划。

规划器只接收裁剪后的目标、记忆和行动摘要，并把模型输出收窄到固定动作集合。模型
不可用、超时、JSON 异常或未知动作时，群聊必须退回 wait，私聊才退回 direct_reply。
新鲜轮次里已经通过频控的全群邀请可直接进入回复；其余消息仍由规划器判断对象和轮次。
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
from ..persona import compose_persona_identity
from ..utils.text import clip_text
from .pfc_utils import get_items_from_json

_logger = logging.getLogger("plugin.xiaoqing_chat")

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
{time_since_last_bot_message_info}{timeout_context}

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
1. 先判断最新消息的说话人、目标对象和它与前文的关系，不把别人之间的互动误当成在叫你
2. 你是活跃的群友，没被点名也可以加入：能补充相关的看法、知识、自己的经历，或接住笑点和情绪时，选择 direct_reply
3. 群友正在接连复读同一句时，跟一句队形算接梗，可以 direct_reply；除此之外只能复述、空泛附和、转述别人之间谁在问谁，或重复追问时，选择 wait 或 listening；面向全群的交流信号不自动等于必须回复，话题已收尾、别人正在一对一说事或你刚连续发过言时也留出空间
4. 对方明确追问你、纠正你或延续你刚才的话时，通常适合继续；明确拒聊、厌烦或转向别人时，应留出空间
5. 消息长度不能单独决定行动：短消息也可能有明确交流作用，长消息也可能与你无关
6. 对不确定的信息先判断是否需要 fetch_knowledge；无需检索时可以承认不确定，但不能编造解释
7. 已经提出而无人回应的问题不要原样追问，按最新话题重新判断是否有必要发言
8. 媒体摘要是消息内容的一部分，应根据它表达的意思和当前上下文判断行动，而不是因为出现媒体就回复

"""
    + _PLANNER_CONTEXT
    + _planner_output_format(
        "fetch_knowledge / listening / direct_reply / wait / rethink_goal / end_conversation / block_and_ignore",
        "你对当前局势的分析：谁在跟谁说话、气氛如何、你该不该插话、为什么",
    )
)

PROMPT_FOLLOW_UP_COMPACT = (
    """{persona_text}。你在QQ{channel}闲聊，刚刚你已经回复过对方。

目标：
{goals_str}

【决策指导——请逐条思考】
1. 先判断最新消息是否在回应、追问或纠正你；如果不是，再判断它是否面向全群以及你能否补充新内容
2. 你刚刚已经发过消息，默认给其他人留出轮次；只有对话确实需要你继续承接时才再次发言。群友接连复读同一句时，跟一句队形属于接梗，不算抢话；你已经跟过同一句就不再跟
3. 不以有没有 @、消息长短或是否包含问号作为单一依据，要综合对象、相关性、时机和新增内容
4. 对方继续交流时可以回应；出现拒聊、厌烦、自然收尾或话题已被别人接住时，选择 wait、listening 或 end_conversation
5. 不重复未获回应的问题，也不换一种说法重复刚才的结论、笑点或行动
6. 不确定的信息可以检索、承认不确定或不接，不要根据零散关键词猜测
7. 媒体摘要与文字具有同等地位，根据其交际含义和上下文判断是否继续

"""
    + _PLANNER_CONTEXT
    + _planner_output_format(
        "fetch_knowledge / wait / listening / send_new_message / rethink_goal / end_conversation / block_and_ignore",
        "你对当前局势的分析：谁在跟谁说话、对方态度如何、你刚发过言该怎么做",
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
    action: str
    reason: str
    thinking: str     = ""
    wait_seconds: int = 0


def _goals_to_text(goal_list: Sequence[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in goal_list[-5:]:
        if not isinstance(item, dict):
            continue
        g = str(item.get("goal", "") or "").strip()
        r = str(item.get("reason", "") or "").strip()
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


def _planner_total_timeout_seconds(secrets: dict[str, Any], timeout_seconds: float) -> float:
    """给 route 中每个候选模型保留一次完整请求预算。"""

    if secrets.get("_pinned_model"):
        profile_count = 1
    else:
        providers = secrets.get("_providers")
        profiles  = (
            {
                str(item.get("profile") or "").strip()
                for item in providers.values()
                if isinstance(item, dict)
            }
            if isinstance(providers, dict)
            else set()
        )
        profiles.discard("")
        profile_count = max(1, len(profiles))
    return min(1800.0, max(0.1, float(timeout_seconds)) * profile_count + 0.3)


def _parse_plan(content: str, fallback_action: str) -> PFCPlan:
    """解析规划器输出；结构不合法或动作不在白名单时退回保守动作。"""
    ok, obj = get_items_from_json(
        content,
        "action",
        "reason",
        optional_items = ("thinking", "wait_seconds"),
        default_values = {
            "action": fallback_action,
            "reason": "",
            "thinking": "",
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

    persona_text       = compose_persona_identity(personality.identity, bot_name)
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
        timeout_context                  = timeout_context.strip(),
        chat_history_text                = chat_history_text,
    )
    if current_text.strip():
        prompt += "\n本轮目标消息：\n" + current_text.strip()

    try:
        t0 = time.monotonic()
        resp, _path = await chat_completions_raw_with_fallback_paths(
            secrets  = secrets,
            messages = [{"role": "user", "content": prompt}],
            **llm.bounded(max_temperature=0.7, max_tokens=400, min_tokens=200).as_kwargs(),
            total_timeout_seconds=_planner_total_timeout_seconds(secrets, llm.timeout_seconds),
        )
        _log_planner_step(
            {
                "step": "pfc.planner.ok",
                "elapsed_s": round(time.monotonic() - t0, 3),
                "model": model,
                "endpoint": _path,
                "prompt_chars": len(prompt),
            }
        )
    except TimeoutError:
        _log_planner_step(
            {
                "step": "pfc.planner.timeout",
                "timeout_s": float(llm.timeout_seconds),
                "model": model,
                "prompt_chars": len(prompt),
            }
        )
        return PFCPlan(action=fallback_action, reason="planner_timeout")
    except Exception as exc:
        _log_planner_step(
            {
                "step": "pfc.planner.error",
                "error_type": type(exc).__name__,
                "timeout_s": float(llm.timeout_seconds),
                "model": model,
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

    persona_text      = compose_persona_identity(personality.identity, bot_name)
    channel           = "私聊" if is_private else "群聊"
    chat_history_text = build_dialogue_prompt(
        history, bot_name=bot_name, truncate=True, max_chars=2200
    )
    prompt = PROMPT_END_DECISION.format(
        persona_text=persona_text, channel=channel, chat_history_text=chat_history_text
    )

    try:
        resp, _ = await chat_completions_raw_with_fallback_paths(
            secrets  = secrets,
            messages = [{"role": "user", "content": prompt}],
            **llm.bounded(max_temperature=0.4, max_tokens=400).as_kwargs(),
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
