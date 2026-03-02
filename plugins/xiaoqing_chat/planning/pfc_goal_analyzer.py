from __future__ import annotations

from typing import Any, Sequence

from ..config.config import PersonalityConfig
from ..llm.llm_client import chat_completions_raw_with_fallback_paths
from ..memory.memory import StoredMessage
from .pfc_utils import get_items_from_json
from ..llm.prompt_builder import build_dialogue_prompt

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
    proxy: str,
    endpoint_path: str,
) -> list[dict[str, Any]]:
    api_base = secrets.get("api_base", "")
    api_key = secrets.get("api_key", "")
    model = secrets.get("model", "")
    if not api_base or not api_key or not model:
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
        goals_str = "目标：目前没有明确对话目标，产生该对话目标的原因：最好思考一个对话目标\n"

    chat_history_text = build_dialogue_prompt(history, bot_name=bot_name, truncate=True, max_chars=2400)
    action_history_text = (action_history_text or "").strip()
    if not action_history_text:
        action_history_text = "（暂无）"

    prompt = (
        f"{persona_text}。现在你在参与一场QQ聊天，请分析以下聊天记录，并根据你的性格特征确定多个明确的对话目标。\n"
        "这些目标应该反映出对话的不同方面和意图。\n\n"
        f"你之前做的事情是：\n{action_history_text}\n\n"
        f"当前对话目标：\n{goals_str}\n"
        f"聊天记录：\n{chat_history_text}\n\n"
        "请分析当前对话并确定最适合的对话目标。你可以：\n"
        "1. 保持现有目标不变\n"
        "2. 修改现有目标\n"
        "3. 添加新目标\n"
        "4. 删除不再相关的目标\n"
        '5. 如果你想结束对话，请设置一个目标，目标goal为"结束对话"，原因reasoning为你希望结束对话\n\n'
        "请以JSON数组格式输出当前的所有对话目标，每个目标包含以下字段：\n"
        "1. goal: 对话目标（简短的一句话）\n"
        "2. reasoning: 对话原因，为什么设定这个目标（简要解释）\n\n"
        "输出格式示例：\n"
        '[{"goal":"回答用户关于Python编程的具体问题","reasoning":"用户提出了技术问题，需要专业且准确的解答"}]'
    )

    try:
        resp, _path = await chat_completions_raw_with_fallback_paths(
            session=http_session,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=min(0.7, float(temperature)),
            top_p=float(top_p),
            max_tokens=min(1200, max(700, int(max_tokens))),
            timeout_seconds=float(timeout_seconds),
            max_retry=int(max_retry),
            retry_interval_seconds=float(retry_interval_seconds),
            proxy=proxy,
            endpoint_path=endpoint_path,
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
