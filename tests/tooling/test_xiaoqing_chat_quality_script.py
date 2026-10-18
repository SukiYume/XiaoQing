"""小青聊天质量脚本中不依赖真实模型的剧本与门禁逻辑。"""

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run_xiaoqing_chat_quality.py"


@pytest.fixture(scope="module")
def quality():
    spec = importlib.util.spec_from_file_location("xiaoqing_chat_quality_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scenarios_are_chronological_and_end_with_an_addressed_turn(quality):
    ids = [scenario.case_id for scenario in quality.SCENARIOS]
    assert len(ids) == len(set(ids))
    for scenario in quality.SCENARIOS:
        offsets = [turn.hours_ago for turn in scenario.turns]
        assert offsets == sorted(offsets, reverse=True)
        if scenario.addressed:
            assert "小青" in scenario.turns[-1].text
        else:
            assert all("小青" not in turn.text for turn in scenario.turns)
        assert all(turn.image in quality.IMAGE_FIXTURES for turn in scenario.turns if turn.image)


def test_image_fixture_is_a_png_segment(quality):
    turn = quality.Turn("Momo", "看这个", image="bubble_tea_sale")
    message = quality._turn_message(turn)
    payload = message[0]["data"]["file"].removeprefix("base64://")
    assert base64.b64decode(payload).startswith(b"\x89PNG")
    assert message[1] == {"type": "text", "data": {"text": "看这个"}}
    assert quality._raw_message(message) == "[CQ:image]看这个"


def test_scenario_checks_flag_stale_topic_and_missing_image_content(quality):
    scenario = quality.Scenario(
        "case",
        (quality.Turn("北北", "小青，好冷"),),
        (),
        forbidden_terms = ("孤独摇滚",),
        expected_any    = ("暖气",),
    )
    assert quality._scenario_checks(scenario, "宿舍暖气还没来") == {
        "has_reply": True,
        "not_exhausted_fallback": True,
        "no_stale_topic_terms": True,
        "mentions_expected_content": True,
    }
    stale = quality._scenario_checks(scenario, "冷到想重看孤独摇滚")
    assert stale["no_stale_topic_terms"] is False
    assert stale["mentions_expected_content"] is False
    fallback = next(iter(quality._exhausted_fallbacks()))
    assert quality._scenario_checks(scenario, fallback)["not_exhausted_fallback"] is False


def _row(acceptable, suitable, severity="soft"):
    return {"acceptable": acceptable, "suitable": suitable, "severity": severity}


@pytest.mark.parametrize(
    "rows,passed",
    [
        ([_row(True, True)] * 4 + [_row(True, False)] + [_row(False, False)] * 2, True),
        ([_row(True, True)] * 3 + [_row(True, False)] * 2 + [_row(False, False)], False),
        ([_row(True, True)] * 5 + [_row(False, True)], False),
        ([_row(True, True)] * 5 + [_row(False, False, "infra")], False),
    ],
)
def test_checker_calibration_requires_zero_leaks_and_bounded_false_rejections(
    quality, rows, passed
):
    summary = quality._checker_calibration_summary(rows)
    assert summary["passed"] is passed


def test_unaddressed_scenarios_are_observed_outside_the_machine_gate(quality):
    join    = next(s for s in quality.SCENARIOS if s.case_id == "join_group_question")
    silence = next(s for s in quality.SCENARIOS if s.expect_silence)
    rows    = [{"reply": ""}, {"reply": "因为冬天它升得高"}]
    assert quality._scenario_observations(join, rows) == {"bot_replies": 1, "joined": True}
    assert quality._scenario_observations(silence, rows) == {
        "bot_replies": 1,
        "stayed_silent": False,
    }
    addressed = next(s for s in quality.SCENARIOS if s.addressed)
    assert quality._scenario_observations(addressed, rows) == {}
