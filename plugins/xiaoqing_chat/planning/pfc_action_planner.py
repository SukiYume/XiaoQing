from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from ..config.config import PersonalityConfig
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..memory.memory import StoredMessage
from .pfc_utils import get_items_from_json
from ..llm.prompt_builder import build_dialogue_prompt

_logger = logging.getLogger("plugin.xiaoqing_chat")

PROMPT_INITIAL_REPLY = """{persona_text}。现在你在参与一场QQ{channel}，请根据以下【所有信息】审慎且灵活的决策下一步行动，可以回复，可以倾听，可以调取知识，甚至可以屏蔽对方：

【当前对话目标】
{goals_str}
{knowledge_info_str}

【最近行动历史概要】
{action_history_summary}
【上一次行动的详细情况和结果】
{last_action_context}
【时间和超时提示】
{time_since_last_bot_message_info}{timeout_context}
【最近的对话记录】(包括你已成功发送的消息 和 新收到的消息。注意：仔细看每条消息的发送者，不同用户是不同的人)
{chat_history_text}

------
【决策指导——请逐条思考】
1. 先分析哪些对话是跟你说的，哪些是其他人之间的互动
2. 逐一评估每个可选动作是否符合当下情境
3. 如果有人在追问你，倾向继续回复；如果有人对你感到厌烦，减少回复
4. 如果相同的action已经被执行过，不要重复执行
5. 若小青之前已经问过某个问题但没有得到回答，跟随最新话题接话

可选行动类型以及解释：
fetch_knowledge: 需要调取专业知识或重要记忆时选择（仅限真正需要的情况，陌生人名/词汇通常不需要）
listening: 倾听对方发言，当你认为对方话才说到一半，发言明显未结束时选择
direct_reply: 直接回复对方
wait: 暂时等待，给对方留出空间（需指定等待秒数）
rethink_goal: 思考一个对话目标，当你觉得目前对话需要目标，或当前目标不再适用，或话题卡住时选择
end_conversation: 结束对话，对方长时间没回复或者当你觉得对话告一段落时可以选择
block_and_ignore: 更加极端的结束对话方式，直接结束对话并在一段时间内无视对方所有发言（屏蔽），当对话让你感到十分不适，或你遭到各类骚扰时选择

请先输出你的思考过程，再输出行动决策。JSON格式：
{{
    "thinking": "你对当前局势的分析：谁在跟谁说话、气氛如何、你该不该插话",
    "action": "选择的行动类型 (必须是上面列表中的一个)",
    "reason": "选择该行动的详细原因",
    "wait_seconds": 0
}}
说明：wait_seconds 仅在 action 为 wait 或 listening 时生效，表示等待秒数（5~120），其余 action 填 0。
注意：请严格按照JSON格式输出，不要包含任何其他内容。"""

PROMPT_INITIAL_REPLY_COMPACT = """{persona_text}。你在QQ{channel}闲聊。

目标：
{goals_str}

【决策指导——请逐条思考】
1. 先分析哪些对话是跟你说的，哪些是其他人之间的互动
2. 逐一评估每个可选动作是否符合当下情境
3. 如果有人在追问你或话题正好和你相关，倾向 direct_reply
4. 如果有人对你感到厌烦、忽视你、或明确表示不想聊，减少回复
5. 你是群里的普通群友，看到有意思的话题可以随口插一句
6. 如果小青已经问过某个问题但没人回答，顺着最新话题接话，不要继续追问
7. 不认识的词/人名直接跳过或随口接话，不要追问含义
{timeout_context}

最近对话（注意：仔细看每条消息的发送者，不同用户是不同的人）：
{chat_history_text}

可选 action（必须选其一）：
fetch_knowledge / listening / direct_reply / wait / rethink_goal / end_conversation / block_and_ignore

请先写出你的思考过程（thinking），再给出行动决策。输出 JSON：
{{
  "thinking": "你对当前局势的分析：谁在跟谁说话、气氛如何、你该不该插话、为什么",
  "action": "选择的行动类型",
  "reason": "选择该行动的原因",
  "wait_seconds": 0
}}
说明：wait_seconds 仅在 action 为 wait 或 listening 时生效，表示等待秒数（5~120），其余 action 填 0。"""

PROMPT_FOLLOW_UP = """{persona_text}。现在你在参与一场QQ{channel}，刚刚你已经回复了对方，请根据以下【所有信息】审慎且灵活的决策下一步行动，可以继续发送新消息，可以等待，可以倾听，可以调取知识，甚至可以屏蔽对方：

【当前对话目标】
{goals_str}
{knowledge_info_str}

【最近行动历史概要】
{action_history_summary}
【上一次行动的详细情况和结果】
{last_action_context}
【时间和超时提示】
{time_since_last_bot_message_info}{timeout_context}
【最近的对话记录】(包括你已成功发送的消息 和 新收到的消息。注意：仔细看每条消息的发送者，不同用户是不同的人)
{chat_history_text}

------
【决策指导——请逐条思考】
1. 先分析哪些对话是跟你说的，哪些是其他人之间的互动
2. 你刚刚已经发过消息，除非对方有新内容，否则优先 wait
3. 如果对方在追问你，继续回复；如果对方显得厌烦，选择 wait 或 end_conversation
4. 如果相同的action已经被执行（如连续 send_new_message），考虑换一种行动
5. 若小青之前已经追问过某个问题但没有得到回答，不要继续追问，顺着最新话题或选 wait

可选行动类型以及解释：
fetch_knowledge: 需要调取专业知识或重要记忆时选择（仅限真正需要的情况，陌生人名/词汇通常不需要）
wait: 暂时不说话，留给对方交互空间，等待对方回复（需指定等待秒数）
listening: 倾听对方发言（如果对方立刻回复且明显话没说完，可以选择这个）
send_new_message: 发送一条新消息继续对话，允许适当的追问、补充、深入话题，或开启相关新话题。避免在因重复被拒后立即使用，也不要过多轰炸
rethink_goal: 思考一个对话目标，当你觉得目前对话需要目标，或当前目标不再适用，或话题卡住时选择
end_conversation: 结束对话，对方长时间没回复或者当你觉得对话告一段落时可以选择
block_and_ignore: 更加极端的结束对话方式，直接结束对话并在一段时间内无视对方所有发言（屏蔽），当对话让你感到十分不适，或你遭到各类骚扰时选择

请先输出你的思考过程，再输出行动决策。JSON格式：
{{
    "thinking": "你对当前局势的分析：谁在跟谁说话、对方态度如何、你刚发过言该怎么做",
    "action": "选择的行动类型 (必须是上面列表中的一个)",
    "reason": "选择该行动的详细原因",
    "wait_seconds": 0
}}
说明：wait_seconds 仅在 action 为 wait 或 listening 时生效，表示等待秒数（5~120），其余 action 填 0。
注意：请严格按照JSON格式输出，不要包含任何其他内容。"""

PROMPT_FOLLOW_UP_COMPACT = """{persona_text}。你在QQ{channel}闲聊，刚刚你已经回复过对方。

目标：
{goals_str}

【决策指导——请逐条思考】
1. 先分析哪些对话是跟你说的，哪些是其他人之间的互动
2. 你刚刚已经发过消息了，除非对方有新内容，否则优先 wait
3. 如果对方在追问你，继续回复；如果对方显得厌烦，选择 wait 或 end_conversation
4. 如果已经追问过某个问题但没人回答，不要继续追问，顺着最新话题或选 wait
5. 不认识的词/人名/英文词不要追问，跳过或随口接话即可
6. 如果相同的行动刚刚已经被执行（如连续 send_new_message），考虑换一种行动
{timeout_context}

最近对话（注意：仔细看每条消息的发送者，不同用户是不同的人）：
{chat_history_text}

可选 action（必须选其一）：
fetch_knowledge / wait / listening / send_new_message / rethink_goal / end_conversation / block_and_ignore

请先写出你的思考过程（thinking），再给出行动决策。输出 JSON：
{{
  "thinking": "你对当前局势的分析：谁在跟谁说话、对方态度如何、你刚发过言该怎么做",
  "action": "选择的行动类型",
  "reason": "选择该行动的原因",
  "wait_seconds": 0
}}
说明：wait_seconds 仅在 action 为 wait 或 listening 时生效，表示等待秒数（5~120），其余 action 填 0。"""

PROMPT_END_DECISION = """{persona_text}。刚刚你决定结束一场 QQ {channel}。

【你们之前的聊天记录】
{chat_history_text}

你觉得你们的对话已经完整结束了吗？有时候，在对话自然结束后再说点什么可能会有点奇怪，但有时也可能需要一条简短的消息来圆满结束。
如果觉得确实有必要再发一条简短、自然、符合你人设的告别消息（比如 "好，下次再聊~" 或 "嗯，先这样吧"），就输出 "yes"。
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
    thinking: str = ""
    wait_seconds: int = 0

def _build_persona_text(bot_name: str, personality: PersonalityConfig) -> str:
    identity = (personality.identity or "").strip()
    if identity:
        return identity.replace("你是", f"你的名字是{bot_name}，你是", 1) if identity.startswith("你是") else f"你的名字是{bot_name}，{identity}"
    return f"你的名字是{bot_name}"

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
        if msg.role != "assistant":
            continue
        diff = max(0.0, now - float(msg.ts or now))
        if diff < 60.0:
            return f"提示：你上一条成功发送的消息是在 {diff:.1f} 秒前。\n"
        return ""
    return ""

async def plan_next_action(
    *,
    http_session,
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
    last_successful_reply_action: Optional[str],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> PFCPlan:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return PFCPlan(action="direct_reply", reason="secrets_missing")

    persona_text = _build_persona_text(bot_name, personality)
    goals_str = _goals_to_text(goal_list) or "- 自然聊天"
    knowledge_info_str = _knowledge_to_text(knowledge_list)
    chat_history_text = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=1200)
    time_info = _time_since_last_bot(history)
    channel = "私聊" if is_private else "群聊"

    tpl = PROMPT_FOLLOW_UP_COMPACT if last_successful_reply_action in ("direct_reply", "send_new_message") else PROMPT_INITIAL_REPLY_COMPACT
    def _truncate(s: str, n: int) -> str:
        t = (s or "").strip()
        if len(t) <= n:
            return t
        return t[: max(0, n - 1)].rstrip() + "…"
    prompt = tpl.format(
        persona_text=persona_text,
        channel=channel,
        goals_str=goals_str,
        knowledge_info_str=(_truncate(knowledge_info_str, 480) + "\n") if knowledge_info_str else "",
        action_history_summary=_truncate(action_history_summary, 360) or "（暂无）",
        last_action_context=_truncate(last_action_context, 360) or "（暂无）",
        time_since_last_bot_message_info=time_info,
        timeout_context=timeout_context.strip(),
        chat_history_text=chat_history_text,
    )

    try:
        t0 = time.monotonic()
        resp, _path = await asyncio.wait_for(
            chat_completions_raw_with_fallback_paths(
                session=http_session,
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=min(0.7, float(temperature)),
                top_p=float(top_p),
                max_tokens=min(400, max(200, int(max_tokens))),
                timeout_seconds=float(timeout_seconds),
                max_retry=int(max_retry),
                retry_interval_seconds=float(retry_interval_seconds),
                proxy=proxy,
                endpoint_path=endpoint_path,
            ),
            timeout=max(0.1, float(timeout_seconds) + 0.3),
        )
        try:
            _logger.info(
                "xiaoqing_chat step=%s",
                json.dumps(
                    {
                        "step": "pfc.planner.ok",
                        "elapsed_s": round(time.monotonic() - t0, 3),
                        "model": model,
                        "endpoint": _path,
                        "prompt_chars": len(prompt),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass
    except asyncio.TimeoutError:
        try:
            _logger.info(
                "xiaoqing_chat step=%s",
                json.dumps({"step": "pfc.planner.timeout", "timeout_s": float(timeout_seconds), "model": model, "prompt_chars": len(prompt)}, ensure_ascii=False),
            )
        except Exception:
            pass
        return PFCPlan(action="wait", reason="planner_timeout")
    except Exception as exc:
        try:
            _logger.info(
                "xiaoqing_chat step=%s",
                json.dumps({"step": "pfc.planner.error", "error": str(exc), "timeout_s": float(timeout_seconds), "model": model}, ensure_ascii=False),
            )
        except Exception:
            pass
        return PFCPlan(action="direct_reply", reason="planner_failed")
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    ok, obj = get_items_from_json(
        str(content),
        "action",
        "reason",
        default_values={"action": "direct_reply", "reason": "", "thinking": "", "wait_seconds": 0},
        required_types={"action": str, "reason": str},
        allow_array=False,
    )
    if not ok or not isinstance(obj, dict):
        return PFCPlan(action="direct_reply", reason="")
    act = str(obj.get("action", "") or "").strip()
    reason = str(obj.get("reason", "") or "").strip()
    thinking = str(obj.get("thinking", "") or "").strip()
    try:
        wait_seconds = int(obj.get("wait_seconds", 0) or 0)
    except (ValueError, TypeError):
        wait_seconds = 0
    wait_seconds = max(0, min(120, wait_seconds))
    return PFCPlan(action=act, reason=reason, thinking=thinking, wait_seconds=wait_seconds)

async def decide_say_bye(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    is_private: bool,
    personality: PersonalityConfig,
    history: Sequence[StoredMessage],
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
    proxy: str,
    endpoint_path: str,
) -> tuple[bool, str]:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
        return False, ""

    persona_text = _build_persona_text(bot_name, personality)
    channel = "私聊" if is_private else "群聊"
    chat_history_text = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=2200)
    prompt = PROMPT_END_DECISION.format(persona_text=persona_text, channel=channel, chat_history_text=chat_history_text)

    try:
        resp, _path = await chat_completions_raw_with_fallback_paths(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=min(0.4, float(temperature)),
            top_p=float(top_p),
            max_tokens=min(400, int(max_tokens)),
            timeout_seconds=float(timeout_seconds),
            max_retry=int(max_retry),
            retry_interval_seconds=float(retry_interval_seconds),
            proxy=proxy,
            endpoint_path=endpoint_path,
        )
    except Exception:
        return False, ""
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    ok, obj = get_items_from_json(
        str(content),
        "say_bye",
        "reason",
        default_values={"say_bye": "no", "reason": ""},
        required_types={"say_bye": str, "reason": str},
        allow_array=False,
    )
    if not ok or not isinstance(obj, dict):
        return False, ""
    val = str(obj.get("say_bye", "") or "").strip().lower()
    return val == "yes", str(obj.get("reason", "") or "").strip()
