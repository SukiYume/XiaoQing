"""真实评测脚本的本地生命周期契约；全部外部执行由替身隔离。"""

import json
from unittest.mock import Mock

import pytest

from scripts import run_xiaoqing_chat_live_eval as evaluation


def test_worker_start_failure_reaps_already_started_workers(tmp_path, monkeypatch):
    worker                   = Mock()
    worker.poll.return_value = None
    start = Mock(side_effect=[worker, KeyboardInterrupt()])
    monkeypatch.setattr(evaluation.subprocess, "Popen", start)
    monkeypatch.setattr(evaluation, "prepare_fixtures", lambda _path: None)
    suites = list(evaluation.SCENARIOS)[:2]
    with pytest.raises(KeyboardInterrupt):
        evaluation.main(
            ["--suite", suites[0], "--suite", suites[1], "--out-dir", str(tmp_path / "run")]
        )
    worker.terminate.assert_called_once_with()
    worker.wait.assert_called_once_with(timeout=10)
    assert all(call.kwargs["stdout"].closed for call in start.call_args_list)


def test_rejudge_uses_saved_expectations_and_media_truth(tmp_path, monkeypatch):
    row  = {"suite": "historical", "expect": "reply", "reply_segments": []}
    path = tmp_path / "historical.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    manifest = {
        "scenarios"  : {"historical": [{"note": "saved expectation"}]},
        "media_truth": {"picture": "saved truth"},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    render = Mock(return_value="saved transcript")
    monkeypatch.setattr(evaluation, "transcript", render)
    monkeypatch.setattr(evaluation, "style_metrics", lambda _rows: {})
    monkeypatch.setattr(evaluation, "rule_findings", lambda _rows: [])
    result = evaluation.judge_run(path, use_judge=False)
    render.assert_called_once_with([row], ["saved expectation"], {"picture": "saved truth"})
    assert result["transcript"] == "saved transcript"
