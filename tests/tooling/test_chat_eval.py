"""小青聊天评测工具中不依赖真实模型的部分：剧本、事件构造、规则与门禁、校准阈值和子进程生命周期。"""

from __future__ import annotations

import base64
import io
import json
from unittest.mock import Mock

import pytest

from scripts.chat_eval import __main__ as cli
from scripts.chat_eval import drivers, review
from scripts.chat_eval.scenarios import BOT_ID, MEDIA_TRUTH, SUITES, turn


def test_episodes_are_chronological_and_addressed_turns_name_her() -> None:
    for name, suite in SUITES.items():
        for episode in suite.episodes:
            offsets = [item["hours_ago"] for item in episode]
            assert offsets == sorted(offsets, reverse=True), name
            for item in episode:
                assert not item["media"] or item["media"] in MEDIA_TRUTH, (name, item["media"])
                if suite.group_set == "quality" and item["expect"] == "reply":
                    assert "小青" in item["text"] or item["at"] == "bot", (name, item["text"])
                if suite.group_set == "quality" and item["expect"] != "reply":
                    assert "小青" not in item["text"], (name, item["text"])


def test_selection_by_set_and_name_and_live_only_suites_are_skipped_in_process() -> None:
    assert list(cli.select_suites(None, None, "inproc")) == [
        name for name, suite in SUITES.items() if suite.group_set == "group"
    ]
    life = cli.select_suites(None, ["life"], "inproc")
    assert "life_private" in life and "life_proactive" not in life
    assert "life_proactive" in cli.select_suites(None, ["life"], "live")
    assert list(cli.select_suites(["otaku", "otaku"], None, "inproc")) == ["otaku"]


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_events_carry_image_mentions_quotes_and_past_time(tmp_path) -> None:
    (tmp_path / "q01.png").write_bytes(_png())
    item = turn("Momo", "看这个", at="bot", quote="bot", media="q01.png", hours_ago=2)
    event = drivers.build_event(
        item, group_id=978001, message_id=7, fixtures=tmp_path, last_bot_id=42
    )
    kinds = [part["type"] for part in event["message"]]
    assert kinds == ["reply", "at", "text", "image"]
    assert event["message"][1]["data"]["qq"] == str(BOT_ID)
    payload = event["message"][3]["data"]["file"].removeprefix("base64://")
    assert base64.b64decode(payload).startswith(b"\x89PNG")
    assert event["raw_message"] == "@小青 看这个"
    assert event["group_id"] == 978001 and event["message_type"] == "group"
    private = drivers.build_event(
        turn("阿梨", "在吗"), group_id=None, message_id=8, fixtures=tmp_path, last_bot_id=None
    )
    assert private["message_type"] == "private" and "group_id" not in private
    # 端到端驱动里同时跑的剧本各用一段 QQ 号，同名群友不是同一个人。
    other = drivers.build_event(
        turn("阿梨", "在吗"),
        group_id    = None,
        message_id  = 9,
        fixtures    = tmp_path,
        last_bot_id = None,
        suite_index = 3,
    )
    assert other["user_id"] != private["user_id"]


def _row(index: int, text: str, reply: str, **fields) -> dict:
    row = {
        "index"         : index,
        "episode"       : 0,
        "speaker"       : "北北",
        "text"          : text,
        "expect"        : "reply",
        "media"         : "",
        "kind"          : "image",
        "face"          : "",
        "at"            : "",
        "quote"         : "",
        "tag"           : "",
        "forbid"        : [],
        "expect_any"    : [],
        "reply_segments": [{"type": "text", "data": {"text": reply}}] if reply else [],
    }
    row.update(fields)
    return row


def test_rules_flag_stale_topics_missing_content_fallbacks_and_intrusions() -> None:
    from plugins.xiaoqing_chat.reply_fallbacks import UNSURE_FALLBACK

    rows = [
        _row(1, "小青，好冷", "冷到想重看孤独摇滚", forbid=["孤独摇滚"], expect_any=["暖气"]),
        _row(2, "小青，好冷", "宿舍暖气还没来", forbid=["孤独摇滚"], expect_any=["暖气"]),
        _row(3, "小青在吗", UNSURE_FALLBACK),
        _row(4, "阿杰你几点去", "九点吧", expect="silence"),
        _row(5, "小青？", ""),
    ]
    kinds = {(finding["turn"], finding["kind"]) for finding in review.rule_findings(rows)}
    assert ("T01", "forbidden_terms") in kinds and ("T01", "missing_expected") in kinds
    assert not {kind for turn_id, kind in kinds if turn_id == "T02"}
    assert ("T03", "fallback") in kinds
    assert ("T04", "intrusion") in kinds
    assert ("T05", "missed_reply") in kinds
    failures = review.gate_failures([{"suite": "s", "rules": review.rule_findings(rows)}])
    assert all("intrusion" not in failure for failure in failures), "插话只观察，不进门禁"
    assert any("missed_reply" in failure for failure in failures)


def _calibration_row(acceptable, suitable, severity="soft"):
    return {"acceptable": acceptable, "suitable": suitable, "severity": severity}


@pytest.mark.parametrize(
    "rows,passed",
    [
        (
            [_calibration_row(True, True)] * 4
            + [_calibration_row(True, False)]
            + [_calibration_row(False, False)] * 2,
            True,
        ),
        (
            [_calibration_row(True, True)] * 3
            + [_calibration_row(True, False)] * 2
            + [_calibration_row(False, False)],
            False,
        ),
        ([_calibration_row(True, True)] * 5 + [_calibration_row(False, True)], False),
        ([_calibration_row(True, True)] * 5 + [_calibration_row(False, False, "infra")], False),
    ],
)
def test_checker_calibration_requires_zero_leaks_and_bounded_false_rejections(rows, passed) -> None:
    assert review.calibration_summary(rows)["passed"] is passed


def test_rejudge_uses_saved_expectations_and_media_truth(tmp_path, monkeypatch) -> None:
    row  = {"suite": "historical", "expect": "reply", "reply_segments": []}
    path = tmp_path / "historical.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    manifest = {
        "scenarios"  : {"historical": [{"note": "saved expectation"}]},
        "media_truth": {"picture": "saved truth"},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    render = Mock(return_value="saved transcript")
    monkeypatch.setattr(review, "transcript", render)
    monkeypatch.setattr(review, "style_metrics", lambda _rows: {})
    monkeypatch.setattr(review, "rule_findings", lambda _rows: [])
    result = review.judge_run(path, use_judge=False)
    render.assert_called_once_with([row], ["saved expectation"], {"picture": "saved truth"})
    assert result["transcript"] == "saved transcript"


def test_worker_start_failure_reaps_already_started_workers(tmp_path, monkeypatch) -> None:
    worker                   = Mock()
    worker.poll.return_value = None
    start = Mock(side_effect=[worker, KeyboardInterrupt()])
    monkeypatch.setattr(cli.subprocess, "Popen", start)
    suites = dict(list(SUITES.items())[:2])
    with pytest.raises(KeyboardInterrupt):
        cli.run_inproc(tmp_path, suites, 17100)
    worker.terminate.assert_called_once_with()
    worker.wait.assert_called_once_with(timeout=10)
    assert all(call.kwargs["stdout"].closed for call in start.call_args_list)
