from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.xiaoqing_chat.config.config import MemoryConfig, PersonalityConfig
from plugins.xiaoqing_chat.experiments.anthropomorphic_group import score_turn
from plugins.xiaoqing_chat.memory.memory import StoredMessage
from plugins.xiaoqing_chat.memory.memory_db import RetrievedItem
from plugins.xiaoqing_chat.memory.memory_retrieval import _query_direct_memory_items
from plugins.xiaoqing_chat.planning.pfc_action_planner import plan_next_action
from plugins.xiaoqing_chat.planning.pfc_engine import PFCRunResult
from plugins.xiaoqing_chat.planning.pfc_utils import get_items_from_json
from plugins.xiaoqing_chat.smalltalk_execution import _execute_planner_wait
from plugins.xiaoqing_chat.utils.json_parsing import (
    parse_first_json_array,
    parse_first_json_object,
)


@pytest.mark.parametrize("last_action", [None, "direct_reply"])
@pytest.mark.asyncio
async def test_compact_planner_prompt_keeps_all_computed_context(
    monkeypatch: pytest.MonkeyPatch,
    last_action: str | None,
) -> None:
    captured: dict = {}

    async def complete(**kwargs):
        captured.update(kwargs)
        return (
            {"choices": [{"message": {"content": '{"action":"direct_reply","reason":"ok"}'}}]},
            "/v1/chat/completions",
        )

    monkeypatch.setattr(
        "plugins.xiaoqing_chat.planning.pfc_action_planner.chat_completions_raw_with_fallback_paths",
        complete,
    )
    await plan_next_action(
        secrets={
            "api_base": "https://example.test",
            "api_key": "k",
            "model": "m",
            "_providers": {
                "fast": {"profile": "fast-profile"},
                "steady": {"profile": "steady-profile"},
            },
        },
        bot_name="小青",
        is_private=False,
        personality=PersonalityConfig(),
        history=[StoredMessage(role="user", name="甲", content="当前消息", ts=1.0)],
        goal_list=[{"goal": "目标哨兵"}],
        knowledge_list=[{"text": "知识哨兵"}],
        action_history_summary="行动概要哨兵",
        last_action_context="上次行动哨兵",
        timeout_context="超时哨兵",
        last_successful_reply_action=last_action,
        temperature=0.5,
        top_p=0.9,
        max_tokens=300,
        timeout_seconds=1.0,
        max_retry=0,
        retry_interval_seconds=0.0,
    )

    prompt = captured["messages"][0]["content"]
    for sentinel in ("目标哨兵", "知识哨兵", "行动概要哨兵", "上次行动哨兵", "超时哨兵"):
        assert sentinel in prompt
    assert captured["total_timeout_seconds"] == pytest.approx(2.3)


@pytest.mark.asyncio
async def test_generation_layer_executes_propagated_planner_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.xiaoqing_chat.smalltalk_execution.asyncio.sleep", sleep)
    result = PFCRunResult(
        reply="",
        action="wait",
        reason="give the user space",
        ended=False,
        wait_seconds=12.5,
    )

    await _execute_planner_wait(result)

    sleep.assert_awaited_once_with(12.5)


def test_direct_memory_query_strictly_reranks_and_limits_top_k() -> None:
    memory_db = MagicMock()
    memory_db.query.return_value = [
        RetrievedItem(doc_id="later", text="later", score=0.2, meta={}),
        RetrievedItem(doc_id="z-tie", text="z", score=0.9, meta={}),
        RetrievedItem(doc_id="a-tie", text="a", score=0.9, meta={}),
        RetrievedItem(doc_id="middle", text="middle", score=0.5, meta={}),
    ]

    items = _query_direct_memory_items(
        memory_db,
        "question",
        cfg=MemoryConfig(top_k=2, min_score=0.1),
        chat_id="g1",
    )

    assert [item.doc_id for item in items] == ["a-tie", "z-tie"]
    assert memory_db.query.call_args.kwargs["top_k"] == 8


def test_planner_json_array_filters_missing_blank_and_wrong_typed_items() -> None:
    ok, result = get_items_from_json(
        """[
          {"action":"reply","reason":"valid","wait":1},
          {"action":"","reason":"blank","wait":2},
          {"action":"wait","wait":3},
          {"action":"reply","reason":"wrong type","wait":"soon"}
        ]""",
        "action",
        "reason",
        required_types={"wait": int},
    )

    assert ok is True
    assert result == [{"action": "reply", "reason": "valid", "wait": 1}]


def test_planner_json_object_preserves_defaults_and_rejects_type_mismatch() -> None:
    ok, result = get_items_from_json(
        '{"action":"reply","wait_seconds":5}',
        "action",
        "reason",
        "wait_seconds",
        default_values={"reason": "default"},
        required_types={"wait_seconds": int},
        allow_array=False,
    )
    assert ok is True
    assert result == {"action": "reply", "reason": "default", "wait_seconds": 5}

    invalid, partial = get_items_from_json(
        '{"action":"reply","reason":"ok","wait_seconds":"five"}',
        "action",
        "reason",
        "wait_seconds",
        required_types={"wait_seconds": int},
        allow_array=False,
    )
    assert invalid is False
    assert partial["action"] == "reply"


def test_planner_json_extractors_accept_fenced_values_without_surrounding_prose() -> None:
    assert parse_first_json_array('```json\n[{"action":"wait"}]\n```') == [{"action": "wait"}]
    assert parse_first_json_object('```json\n{"action":"reply"}\n```') == {"action": "reply"}
    assert parse_first_json_object('prefix {"action":"reply"}') is None


def _turn(*, expected_action: str = "reply", with_input_image: bool = False) -> dict:
    segments = [{"type": "text", "data": {"text": "小青看看"}}]
    if with_input_image:
        segments.append({"type": "image", "data": {"file": "input.png"}})
    return {
        "expected_action": expected_action,
        "rubric_tags": [],
        "message_segments": segments,
    }


def test_anthropomorphic_score_marks_unobserved_dimensions_na() -> None:
    result = score_turn(
        _turn(),
        [{"type": "text", "data": {"text": "我觉得挺有意思的"}}],
    )

    assert result["scores"]["context_understanding"] is None
    assert result["scores"]["topic_tracking"] is None
    assert result["scores"]["persona_consistency"] is None
    assert "context_understanding" in result["applicable_dimensions"]
    assert "context_understanding" not in result["observed_dimensions"]
    assert result["evidence"]["context_understanding"].startswith("not observed")


def test_anthropomorphic_score_does_not_award_default_media_score_for_input() -> None:
    result = score_turn(
        _turn(with_input_image=True),
        [{"type": "text", "data": {"text": "这是一张猫猫图片"}}],
    )

    assert result["scores"]["multimodal_natural"] is None
    assert "multimodal_natural" not in result["observed_dimensions"]


def test_anthropomorphic_score_uses_actual_bot_media_output() -> None:
    result = score_turn(
        _turn(with_input_image=False),
        [
            {"type": "text", "data": {"text": "这是一张猫猫图片"}},
            {"type": "image", "data": {"file": "bot-output.png"}},
        ],
    )

    assert result["scores"]["multimodal_natural"] == 2
    assert "media_mechanical" in result["failure_tags"]
    assert result["status"] == "REVIEW"


def test_missing_reply_average_uses_only_observed_dimensions() -> None:
    result = score_turn(_turn(with_input_image=True), [])

    assert result["status"] == "REVIEW"
    assert result["scores"]["trigger_reasonable"] == 1
    assert result["scores"]["multimodal_natural"] is None
    assert result["average"] == 2.0
