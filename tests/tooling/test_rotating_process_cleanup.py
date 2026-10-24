"""日志泵清理时完整聚合进程与管道错误。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import run_process_with_rotating_logs as runner


@pytest.mark.parametrize("raises", [False, True])
def test_process_termination_failure_still_closes_both_pipes(monkeypatch, raises):
    failure = OSError("termination failed")
    closed  = []
    process = SimpleNamespace(
        stdout=SimpleNamespace(close=lambda: closed.append("stdout")),
        stderr=SimpleNamespace(close=lambda: closed.append("stderr")),
    )

    def terminate(received):
        assert received is process
        if raises:
            raise failure
        return 1, failure

    monkeypatch.setattr(runner, "_terminate_process_tree", terminate)
    errors = []
    runner._stop_process_and_close_pipes(process, errors)

    assert errors == [failure]
    assert closed == ["stdout", "stderr"]


def test_pipe_close_failure_still_closes_other_pipe_and_joins_threads(monkeypatch):
    failure = OSError("stdout close failed")
    calls   = []

    def close_stdout():
        calls.append("stdout")
        raise failure

    process = SimpleNamespace(
        stdout=SimpleNamespace(close=close_stdout),
        stderr=SimpleNamespace(close=lambda: calls.append("stderr")),
    )
    monkeypatch.setattr(runner, "_terminate_process_tree", lambda process: (0, None))
    monkeypatch.setattr(runner, "_join_pump_threads", lambda *args: calls.append("join"))
    errors = []
    runner._cleanup_owned_process(process, [], errors)

    assert errors == [failure]
    assert calls == ["stdout", "stderr", "join"]


def test_stalled_pump_retries_join_after_process_and_pipe_cleanup(monkeypatch):
    calls  = []
    thread = SimpleNamespace(
        name     = "stdout-pump",
        join     = lambda timeout: calls.append("join"),
        is_alive = lambda: calls.count("join") < 2,
    )
    process = SimpleNamespace(
        stdout=SimpleNamespace(close=lambda: calls.append("stdout")),
        stderr=SimpleNamespace(close=lambda: calls.append("stderr")),
    )

    def terminate(received):
        assert received is process
        calls.append("terminate")
        return 0, None

    monkeypatch.setattr(runner, "_terminate_process_tree", terminate)
    errors = []
    runner._join_pump_threads([thread], process, errors)

    assert errors == []
    assert calls == ["join", "terminate", "stdout", "stderr", "join"]
