"""规划器也看得到她记得的这个人和这个群；一轮只取一次记忆，规划和生成共用。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.xiaoqing_chat import reply_plan
from plugins.xiaoqing_chat.config.config import PersonalityConfig, XiaoQingChatConfig
from plugins.xiaoqing_chat.llm.llm_config import LLMCallConfig
from plugins.xiaoqing_chat.planning import pfc_action_planner
from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action

_LLM = LLMCallConfig(
    temperature            = 0.7,
    top_p                  = 0.9,
    max_tokens             = 256,
    timeout_seconds        = 1.0,
    max_retry              = 0,
    retry_interval_seconds = 0.0,
)


async def _planner_prompt(monkeypatch, memory_text: str) -> str:
    seen: list[str] = []

    async def fake_chat(*, messages, **_kwargs):
        seen.append(messages[0]["content"])
        content = json.dumps({"thinking": "", "action": "wait", "reason": "", "wait_seconds": 0})
        return {"choices": [{"message": {"content": content}}]}, "responses"

    monkeypatch.setattr(pfc_action_planner, "chat_completions_raw_with_profile", fake_chat)
    await plan_next_action(
        secrets                      = {},
        bot_name                     = "小青",
        is_private                   = False,
        personality                  = PersonalityConfig(),
        history                      = [],
        goal_list                    = [],
        knowledge_list               = [],
        action_history_summary       = "",
        last_action_context          = "",
        timeout_context              = "",
        last_successful_reply_action = None,
        llm                          = _LLM,
        current_text                 = "在吗",
        memory_text                  = memory_text,
    )
    return seen[0]


@pytest.mark.asyncio
async def test_planner_sees_what_she_remembers_about_the_speaker_and_group(monkeypatch) -> None:
    memory = "你记得的关于阿梨的事：\n- [和你之间] 你们约好周末一起追新番"
    prompt = await _planner_prompt(monkeypatch, memory)
    assert "【你记得的人和群】" in prompt and "你们约好周末一起追新番" in prompt
    assert "【你记得的人和群】" not in await _planner_prompt(monkeypatch, "")


class _CountingMemory:
    """统一记忆能力的替身：记下每次 recall 的请求，回答固定的一页。"""

    def __init__(self, result: dict) -> None:
        self.result              = result
        self.recalls: list[dict] = []

    async def recall(self, payload: dict) -> dict:
        self.recalls.append(payload)
        return self.result


@pytest.mark.asyncio
async def test_one_recall_per_turn_is_shared_by_planning_and_drafting(tmp_path) -> None:
    speaker_block = "你记得的关于阿梨的事：\n- 爱吃螺蛳粉"
    memory        = _CountingMemory({"blocks": {"speaker": speaker_block}})
    runtime = SimpleNamespace(cfg=XiaoQingChatConfig())
    context = SimpleNamespace(
        capabilities=SimpleNamespace(memory=memory),
        data_dir=tmp_path,
    )
    event  = {"user_id": 7, "group_id": 1, "sender": {"card": "阿梨"}}
    kwargs = {"chat_id": "g1", "event": event, "text": "在吗", "history": []}

    first  = await reply_plan.recall_for_reply(context, runtime, **kwargs)
    second = await reply_plan.recall_for_reply(context, runtime, **kwargs)

    assert first is second and len(memory.recalls) == 1
    assert reply_plan.planner_memory(runtime, first) == speaker_block
