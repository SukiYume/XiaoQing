from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..config.config import PlannerConfig
from .actions_registry import build_action_options_text, get_action_specs
from ..memory.memory import StoredMessage
from ..llm.prompt_builder import ChatMessage, build_dialogue_prompt

@dataclass(frozen=True)
class PlannedAction:
    action: str
    target_message_id: str
    think_level: int
    quote: bool
    reasoning: str
    question: str
    unknown_words: list[str]
    params: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class PlanResult:
    reasoning: str
    actions: list[PlannedAction]

@dataclass(frozen=True)
class PlannerContext:
    freq_state: str
    recent_actions: str
    mentioned: bool

_RE_JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)

_PLANNER_SYSTEM = (
    "你是群聊里的观察者和规划器。你需要判断：此时你（小青）要不要回复。\n"
    "你不要真的生成回复内容，只输出规划 JSON。\n"
    "规则：\n"
    "- 被点名/被@时一般要回复。\n"
    "- 群聊里可以不每句都接话，避免刷屏。\n"
    "- 如果对方只是发了无意义的噪声/表情/重复内容，可忽略。\n"
    "- 如果需要查证/回忆某个事实，可以提出一个最关键的问题用于检索。\n"
    "- 如果出现黑话/缩写/不明词，列出 unknown_words。\n"
)

def build_planner_messages(
    *,
    bot_name: str,
    sender_name: str,
    history: Sequence[StoredMessage],
    current_text: str,
    cfg: PlannerConfig,
    ctx: PlannerContext,
) -> list[ChatMessage]:
    dialogue = build_dialogue_prompt(history, bot_name=bot_name, truncate=True)
    rules = []
    if ctx.mentioned and cfg.mentioned_bot_reply:
        rules.append("这条消息明显是在叫你，你更倾向于回复。")
    if cfg.smooth > 0:
        rules.append(f"请尽量让回复行为更平滑，不要每条都回复（平滑度={cfg.smooth}）。")
    think_mode = cfg.resolve_think_level(len(history))
    quote_text = ""
    if cfg.llm_quote:
        quote_text = '  "quote": true,\n'
    action_specs = get_action_specs(quote_enabled=bool(cfg.llm_quote))
    action_options_text = build_action_options_text(action_specs)

    user = (
        "最近对话如下（你是“{bot}(你)”）：\n"
        "{dialogue}\n\n"
        "现在 {sender} 说：{text}\n\n"
        "{rules}\n"
        "{freq_state}\n"
        "{recent_actions}\n"
        "可选的 action：\n"
        "{action_options}\n\n"
        "target_message_id 必填，格式为 m+数字（例如 m012），只能从对话中出现过的消息ID里选。\n"
        "你可以输出多个 action，每行一个 JSON，放在同一个 ```json 代码块里。\n"
        "先输出一行很短的理由文本（不要分点），再输出 ```json。\n"
        "输出格式示例：\n"
        "理由文本（简短）\n"
        "```json\n"
        "{{\"action\":\"reply\",\"target_message_id\":\"m012\",\"think_level\":{think_level},{quote_text}\"unknown_words\":[],\"question\":\"\"}}\n"
        "{{\"action\":\"no_reply\"}}\n"
        "```\n"
        "现在请输出：\n"
        '{{\n'
        '  "action": "reply|no_reply",\n'
        '  "target_message_id": "m012",\n'
        '  "think_level": {think_level},\n'
        "{quote_text}"
        '  "reasoning": "一句话说明为什么",\n'
        '  "question": "如果需要检索/回忆，写一个最关键问题；否则为空字符串",\n'
        '  "unknown_words": ["不明词1", "不明词2"]\n'
        '}}\n'
    ).format(
        bot=bot_name,
        dialogue=dialogue,
        sender=sender_name or "用户",
        text=current_text.strip(),
        rules=("\n".join(rules) + "\n") if rules else "",
        freq_state=(ctx.freq_state.strip() + "\n") if ctx.freq_state.strip() else "",
        recent_actions=(ctx.recent_actions.strip() + "\n") if ctx.recent_actions.strip() else "",
        think_level=think_mode,
        quote_text=quote_text,
        action_options=action_options_text,
    )

    return [
        ChatMessage(role="system", content=_PLANNER_SYSTEM.strip()),
        ChatMessage(role="user", content=user.strip()),
    ]

def _extract_reasoning_and_json_lines(text: str) -> tuple[str, list[str]]:
    if not text:
        return "", []
    blocks = _RE_JSON_BLOCK.findall(text)
    if not blocks:
        return "", []
    idx = text.lower().find("```json")
    if idx == -1:
        idx = text.find("```")
    reasoning = text[:idx].strip() if idx != -1 else ""
    lines: list[str] = []
    for b in blocks:
        for line in b.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("//"):
                continue
            if "{" in line and "}" in line:
                lines.append(line)
    return reasoning, lines

def _parse_single_action(obj: dict[str, Any], *, default_think_level: int, default_target: str) -> Optional[PlannedAction]:
    action = str(obj.get("action", "")).strip().lower()
    if action in ("ignore", "silent", "no_reply"):
        return PlannedAction(
            action="no_reply",
            target_message_id="",
            think_level=0,
            quote=False,
            reasoning="",
            question="",
            unknown_words=[],
        )
    if action == "ask_user":
        target = str(obj.get("target_message_id", "") or default_target).strip()
        quote = bool(obj.get("quote", False))
        reasoning = str(obj.get("reasoning", "") or "").strip()
        ask = str(obj.get("ask", "") or "").strip()
        if not ask:
            ask = str(obj.get("question", "") or "").strip()
        return PlannedAction(
            action="ask_user",
            target_message_id=target,
            think_level=0,
            quote=quote,
            reasoning=reasoning,
            question="",
            unknown_words=[],
            params={"ask": ask},
        )
    if action != "reply":
        return None
    target = str(obj.get("target_message_id", "") or default_target).strip()
    think_level = obj.get("think_level", default_think_level)
    try:
        think_level = int(think_level)
    except Exception:
        think_level = int(default_think_level)
    think_level = 0 if think_level < 0 else (2 if think_level > 2 else think_level)
    quote = bool(obj.get("quote", False))
    reasoning = str(obj.get("reasoning", "") or "").strip()
    question = str(obj.get("question", "") or "").strip()
    unknown_raw = obj.get("unknown_words", [])
    unknown_words: list[str] = []
    if isinstance(unknown_raw, list):
        for item in unknown_raw:
            if isinstance(item, str) and item.strip():
                unknown_words.append(item.strip())
    return PlannedAction(
        action="reply",
        target_message_id=target,
        think_level=think_level,
        quote=quote,
        reasoning=reasoning,
        question=question,
        unknown_words=unknown_words,
        params={},
    )

def parse_plan(text: str, *, default_think_level: int = 1, default_target: str = "") -> PlanResult:
    reasoning, json_lines = _extract_reasoning_and_json_lines(text)
    actions: list[PlannedAction] = []

    if json_lines:
        for line in json_lines:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            act = _parse_single_action(obj, default_think_level=default_think_level, default_target=default_target)
            if act:
                actions.append(act)
    else:
        s = (text or "").strip()
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                obj = json.loads(s[start : end + 1])
            except Exception:
                obj = {}
            if isinstance(obj, dict):
                act = _parse_single_action(obj, default_think_level=default_think_level, default_target=default_target)
                if act:
                    actions.append(act)

    if not actions:
        actions = [
            PlannedAction(
                action="reply",
                target_message_id=default_target,
                think_level=int(default_think_level),
                quote=False,
                reasoning="",
                question="",
                unknown_words=[],
            )
        ]

    return PlanResult(reasoning=reasoning.strip(), actions=actions)
