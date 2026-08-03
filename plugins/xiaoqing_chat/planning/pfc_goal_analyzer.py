from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..config.config import PersonalityConfig
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..llm.prompt_builder import build_dialogue_prompt
from ..memory.memory import StoredMessage
from .pfc_utils import get_items_from_json


async def analyze_goals(
    *,
    http_session,
    secrets: dict[str, Any],
    bot_name: str,
    personality: PersonalityConfig,
    history: Sequence[StoredMessage],
    current_goal_list: Sequence[dict[str, Any]],
    action_history_text: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
    timeout_seconds: float,
    max_retry: int,
    retry_interval_seconds: float,
) -> list[dict[str, Any]]:
    if "_ai" in secrets and secrets.get("_ai") is None:
        return list(current_goal_list)

    identity = (personality.identity or "").strip()
    persona_text = f"你的名字是{bot_name}，{identity}" if identity else f"你的名字是{bot_name}"

    goals_str = ""
    if current_goal_list:
        for item in current_goal_list[-5:]:
            if isinstance(item, dict):
                g = str(item.get("goal", "") or "").strip() or "目标内容缺失"
                r = str(item.get("reasoning", "") or "").strip() or "没有明确原因"
            else:
                g = str(item).strip() or "目标内容缺失"
                r = "没有明确原因"
            goals_str += f"目标：{g}，产生该对话目标的原因：{r}\n"
    else:
        goals_str = "目前没有明确对话目标。\n"

    chat_history_text = build_dialogue_prompt(
        history, bot_name=bot_name, truncate=True, max_chars=2400
    )
    action_history_text = (action_history_text or "").strip()
    if not action_history_text:
        action_history_text = "（暂无）"

    prompt = (
        f"{persona_text}。现在你在参与一场QQ聊天，请根据可见对话确定当前真正需要处理的对话目标。\n"
        "目标必须来自聊天中的明确需求或正在延续的互动，不能替对方补写意图，也不要为了显得积极而制造新目标。\n"
        "保留最少且仍然相关的目标；普通闲聊可以只有一个宽松目标。\n\n"
        f"你之前做的事情是：\n{action_history_text}\n\n"
        f"当前对话目标：\n{goals_str}\n"
        f"聊天记录：\n{chat_history_text}\n\n"
        "请分析当前对话并更新目标。你可以：\n"
        "1. 保持现有目标不变\n"
        "2. 修改现有目标\n"
        "3. 添加新目标\n"
        "4. 删除不再相关的目标\n"
        '5. 如果你想结束对话，请设置一个目标，目标goal为"结束对话"，原因reasoning为你希望结束对话\n\n'
        "请以 JSON 数组输出当前仍成立的目标，每个目标包含以下字段：\n"
        "1. goal: 对话目标（简短的一句话）\n"
        "2. reasoning: 目标所依据的当前对话证据（简要解释）\n\n"
        '只输出 JSON 数组，结构为：[{"goal":"...","reasoning":"..."}]'
    )

    try:
        resp, _path = await chat_completions_raw_with_fallback_paths(
            secrets=secrets,
            messages=[{"role": "user", "content": prompt}],
            temperature=min(0.7, float(temperature)),
            top_p=float(top_p),
            max_tokens=min(1200, max(700, int(max_tokens))),
            timeout_seconds=float(timeout_seconds),
            max_retry=int(max_retry),
            retry_interval_seconds=float(retry_interval_seconds),
        )
    except Exception:
        return list(current_goal_list)
    content = (((resp.get("choices") or [{}])[0] or {}).get("message") or {}).get("content") or ""
    ok, result = get_items_from_json(
        str(content),
        "goal",
        "reasoning",
        required_types={"goal": str, "reasoning": str},
        allow_array=True,
    )
    if ok and isinstance(result, list):
        out: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            g = str(item.get("goal", "") or "").strip()
            r = str(item.get("reasoning", "") or "").strip()
            if not g or not r:
                continue
            out.append({"goal": g, "reasoning": r})
        return out[:5] if out else list(current_goal_list)
    return list(current_goal_list)
