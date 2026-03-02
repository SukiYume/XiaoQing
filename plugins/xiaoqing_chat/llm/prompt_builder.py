from __future__ import annotations

import re
import time
import random
from dataclasses import dataclass
from typing import Any, Sequence

from ..config.config import PersonalityConfig
from ..memory.memory import StoredMessage
from ..helper_utils import _extract_sender_name

@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

_DEFAULT_REPLYER_SYSTEM = (
    "你正在群里聊天，读读之前的聊天记录，把握当前的话题，然后给出日常且简短的回复。\n"
    "最好一次对一个话题进行回复，免得啰嗦或者回复内容太乱。\n"
    "口语化、像真人随口接话；允许有点情绪、吐槽、犹豫，不需要每句都完美。\n"
    "遇到不认识的词/英文/人名/梗，随口接一句或直接忽略就好。\n"
    "如果之前问过的问题没人回答，就放下它，跟着最新的话题走。\n"
    "不要输出多余前后缀，不要用括号包裹解释，不要 @ 任何人。\n"
    "不要主动强调自己是机器人/AI。被问到时自然回应即可。\n"
    "只输出你要发的那段话。\n"
)

def _format_message_time(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return time.strftime("%H:%M", time.localtime())

def _maybe_truncate_message(text: str, *, ratio: float) -> str:
    if ratio < 0.2:
        limit, suffix = 50, "……（记不清了）"
    elif ratio < 0.5:
        limit, suffix = 100, "……（有点记不清了）"
    elif ratio < 0.7:
        limit, suffix = 200, "……（内容太长了）"
    else:
        limit, suffix = 400, "……（内容太长了）"
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + suffix

def build_dialogue_prompt(
    history: Sequence[StoredMessage],
    *,
    bot_name: str,
    truncate: bool = True,
    max_chars: int = 800,
) -> str:
    lines: list[str] = []
    total = 0
    truncated = False

    items = list(history)
    if truncate and len(items) > 12:
        items = items[-12:]
        truncated = True

    last_ts: float = 0.0
    for idx, msg in enumerate(items):
        name = msg.name.strip() if msg.name else ""
        if msg.role == "assistant":
            name = f"{bot_name}(你)"
        elif not name:
            name = "用户"

        msg_ts = float(getattr(msg, "ts", 0.0) or 0.0)
        if last_ts and msg_ts:
            gap = msg_ts - last_ts
            if gap > 8 * 3600:
                hours = int(gap // 3600)
                lines.append(f"——距离上一条消息过去了{hours}小时——")
        if msg_ts:
            last_ts = msg_ts
        id_text = (getattr(msg, "local_id", "") or "").strip() or (
            f"m{msg.message_id}" if getattr(msg, "message_id", None) is not None else ""
        )
        prefix = f"[{id_text}]" if id_text else ""
        t = _format_message_time(msg_ts) if msg_ts else ""
        text = (msg.content or "").strip()
        if not text:
            continue
        if truncate:
            ratio = idx / max(1, len(items) - 1)
            new_text = _maybe_truncate_message(text, ratio=ratio)
            if new_text != text:
                truncated = True
                text = new_text
        line = f"{prefix}{t}, {name}: {text}".strip()
        if truncate and total + len(line) > max_chars and lines:
            truncated = True
            break
        lines.append(line)
        total += len(line)

    if truncated:
        lines.insert(0, "（前面的有点记不清了…）")

    return "\n".join(lines).strip()



def build_prompt_messages(
    *,
    is_private: bool,
    bot_name: str,
    sender_name: str,
    think_level: int,
    history: Sequence[StoredMessage],
    current_text: str,
    personality: PersonalityConfig,
    keyword_rules: list[Any],
    regex_rules: list[Any],
    memory_block: str = "",
    expression_habits_block: str = "",
    jargon_explanation: str = "",
    tool_info_block: str = "",
    planner_reasoning: str = "",
    identity_block: str = "",
    reply_style_override: str = "",
    state_override: str = "",
    request_id: str,
) -> list[ChatMessage]:
    sender = sender_name.strip() if sender_name else "用户"
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    guardrail = ""
    if personality.polite_guardrail:
        guardrail = (
            "你不会辱骂人，也不要使用攻击性语言。不要辱骂、不要人身攻击、不要恶意挑衅。\n"
        )

    style = reply_style_override.strip() if reply_style_override else (personality.reply_style.strip() if personality.reply_style else "")
    channel = "私聊" if is_private else "群聊"
    identity = identity_block.strip() if identity_block else personality.identity.strip()
    # 使用持久化的情绪状态（若有），否则随机抽取
    state_text = state_override.strip() if state_override else ""
    if not state_text and personality.states and random.random() < max(0.0, min(1.0, personality.state_probability)):
        state_text = random.choice(personality.states).strip()

    system_lines: list[str] = []
    system_lines.append(f"你正在 QQ {channel}里聊天。你是「{bot_name}」。")
    if identity:
        system_lines.append(identity)
    if state_text:
        system_lines.append(state_text)
    system_lines.append(_DEFAULT_REPLYER_SYSTEM.strip())
    if guardrail.strip():
        system_lines.append(guardrail.strip())
    if expression_habits_block.strip():
        system_lines.append(expression_habits_block.strip())
    if tool_info_block.strip():
        system_lines.append(tool_info_block.strip())
    if memory_block.strip():
        system_lines.append(memory_block.strip())
    if jargon_explanation.strip():
        system_lines.append(jargon_explanation.strip())
    if style:
        system_lines.append("回复风格偏好\n" + style)
    system_lines.append(f"当前时间\n{now}")
    system_lines.append(f"请求ID\n{request_id}")
    system_prompt = "\n\n".join([s for s in system_lines if s]).strip()

    dialogue = build_dialogue_prompt(history, bot_name=bot_name, truncate=True)
    chat_target = "下面是你们的对话" if is_private else "下面是群里正在聊的内容"
    user_blocks: list[str] = []
    # Planner reasoning first — gives the LLM context on *why* it's replying
    if planner_reasoning.strip():
        user_blocks.append("你为什么要回复这条消息\n" + planner_reasoning.strip())
    user_blocks.append(f'{chat_target}（注意：你是"{bot_name}(你)"）\n{dialogue}'.strip())
    user_blocks.append(f"现在 {sender} 说\n{current_text.strip()}".strip())
    user_blocks.append(f"你准备回复给 {sender}。只输出你要发的那段话。")

    reaction_prompts: list[str] = []
    for item in keyword_rules or []:
        if isinstance(item, dict):
            kw = str(item.get("keyword", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
        else:
            kw = str(getattr(item, "keyword", "")).strip()
            prompt = str(getattr(item, "prompt", "")).strip()
        if kw and prompt:
            reaction_prompts.append(prompt)
    for item in regex_rules or []:
        if isinstance(item, dict):
            pat = str(item.get("pattern", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
        else:
            pat = str(getattr(item, "pattern", "")).strip()
            prompt = str(getattr(item, "prompt", "")).strip()
        if pat and prompt:
            reaction_prompts.append(prompt)
    if reaction_prompts:
        user_blocks.append("关键词反应（可参考，不要生硬照抄）\n" + "；".join([p for p in reaction_prompts if p]))

    user_prompt = re.sub(r"\n{3,}", "\n\n", "\n\n".join([b for b in user_blocks if b]).strip()).strip()

    msgs = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_prompt),
    ]

    if think_level >= 2:
        msgs.append(ChatMessage(role="user", content="思考可以更深入一点，但别写出思考过程。"))

    return msgs
