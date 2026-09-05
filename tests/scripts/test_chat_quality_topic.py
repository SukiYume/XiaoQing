"""质量产物区分机器结构门禁与独立人工语义审阅。"""

from types import SimpleNamespace

import pytest

from scripts import run_xiaoqing_chat_quality as quality


@pytest.mark.parametrize("reply", ["你觉得呢？", "图片里有一只猫。", "叼着烟很有格调。"])
def test_automatic_checks_do_not_claim_semantic_acceptance(reply):
    assert quality._automatic_checks("stale_topic", reply) == {"has_reply": True, "concise": True}
    review = quality._semantic_review_material(
        "stale_topic", "当前图片话题", reply, prior_message="旧消息"
    )
    assert review["status"] == "pending"
    assert review["verdict"] is None
    assert review["reviewer"] is None
    assert review["reply"] == reply
    assert review["message"] == "当前图片话题"
    assert review["prior_message"] == "旧消息"
    assert len(review["criteria"]) == 3


@pytest.mark.parametrize("reply,expected", [("", False), ("字" * 257, False), ("字" * 256, True)])
def test_machine_length_contract_remains_explicit(reply, expected):
    assert quality._automatic_checks("direct_without_question", reply)["concise"] is expected


def test_no_followup_requirement_is_preserved_for_human_review():
    review = quality._semantic_review_material("direct_without_question", "别反问", "你觉得呢？")
    assert any("不追问" in criterion for criterion in review["criteria"])
    assert review["verdict"] is None


@pytest.mark.parametrize("request_ok", [True, False])
def test_suite_keeps_legacy_gate_as_machine_only_and_requires_review(
    monkeypatch, tmp_path, request_ok
):
    case = {
        "reply": "你觉得呢？",
        "checks": {"has_reply": True, "concise": True},
        "status": 200 if request_ok else 503,
        "error": None,
        "cleanup": {"ok": True},
    }
    monkeypatch.setattr(quality, "_run_forced_cases", lambda _probe: [case])
    monkeypatch.setattr(
        quality,
        "_run_participation_case",
        lambda _probe: {
            "all_requests_ok": True,
            "all_cleanups_ok": True,
            "replied_within_three_cues": True,
        },
    )
    monkeypatch.setattr(
        quality,
        "_run_stale_topic_case",
        lambda *_args: {
            "checks": {"full_store_keeps_current_turn": True},
            "all_requests_ok": True,
            "all_cleanups_ok": True,
        },
    )
    probe = SimpleNamespace(endpoint="http://localhost/event", timeout=1, message_id_seed=1)
    report = quality._run_quality_suite(probe, tmp_path)
    assert report["schema_version"] == 2
    assert report["gate_passed"] is request_ok
    assert report["machine_gate_passed"] is request_ok
    assert report["semantic_review_required"] is True
    assert report["semantic_review_status"] == "pending"
    assert report["semantic_acceptance_passed"] is None
    assert report["aggregate"]["question_endings"] == 1
