from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    parameters: dict[str, str]
    require: str

def get_action_specs(*, quote_enabled: bool) -> list[ActionSpec]:
    specs = [
        ActionSpec(
            name="reply",
            description="回复一条消息。可带 unknown_words/question，用于后续检索与解释。",
            parameters={
                "target_message_id": "必填：触发消息ID（m+数字）",
                "think_level": "0/1/2",
                "unknown_words": "字符串数组",
                "question": "字符串",
                "quote": "布尔（可选）" if quote_enabled else "布尔（可选，通常省略）",
            },
            require="不要回复你自己发送的消息；避免只对表情/噪声回复。",
        ),
        ActionSpec(
            name="no_reply",
            description="保持沉默，不回复直到有新消息。",
            parameters={},
            require="用于控频、噪声过滤、或当前不适合插话。",
        ),
        ActionSpec(
            name="ask_user",
            description="提一个简短澄清问题，而不是直接给出回答。",
            parameters={
                "target_message_id": "必填：触发消息ID（m+数字）",
                "ask": "要问用户的一句话（尽量短）",
                "quote": "布尔（可选）" if quote_enabled else "布尔（可选，通常省略）",
            },
            require="当上下文缺关键信息、需要用户补充时使用。",
        ),
    ]
    return specs

def build_action_options_text(specs: Sequence[ActionSpec]) -> str:
    lines: list[str] = []
    for s in specs:
        lines.append(f"{s.name}")
        lines.append(f"动作描述：{s.description}")
        lines.append(f"使用条件：{s.require}")
        if s.parameters:
            params = ", ".join(f"\"{k}\":{v}" for k, v in s.parameters.items())
        else:
            params = ""
        if params:
            lines.append(f"示例：{{\"action\":\"{s.name}\",{params}}}")
        else:
            lines.append(f"示例：{{\"action\":\"{s.name}\"}}")
        lines.append("")
    return "\n".join(lines).strip()
