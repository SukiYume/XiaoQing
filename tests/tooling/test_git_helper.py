"""临时 Git 仓库测试工具的重试与错误诊断回归测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tests.helpers.git as git_helper
from tests.helpers.git import GitCommandError, run_git


def test_run_git_retries_one_known_index_lock_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        [
            subprocess.CompletedProcess(
                [],
                128,
                "",
                "fatal: Unable to create '.git/index.lock': File exists.",
            ),
            subprocess.CompletedProcess([], 0, "abc123\n", ""),
        ]
    )
    calls: list[list[str]]             = []
    environments: list[dict[str, str]] = []
    sleeps: list[float]                = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        environments.append(environment)
        return next(results)

    monkeypatch.setenv("GIT_INDEX_FILE", "outer-index-must-not-leak")
    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    monkeypatch.setattr(git_helper.time, "sleep", sleeps.append)

    assert run_git(tmp_path, "add", "-A") == "abc123"
    assert len(calls) == 2
    assert all("GIT_INDEX_FILE" not in environment for environment in environments)
    assert sleeps == [git_helper._TRANSIENT_RETRY_DELAYS[0]]


def test_run_git_does_not_retry_unclassified_exit_128(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(
        _command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess([], 128, "partial output", "fatal: bad revision")

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)

    with pytest.raises(GitCommandError) as exc_info:
        run_git(tmp_path, "show", "missing")

    message = str(exc_info.value)
    assert calls == 1
    assert "exit code 128" in message
    assert "fatal: bad revision" in message
    assert "partial output" in message
    assert exc_info.value.git_args == ("show", "missing")


def test_run_git_bounds_diagnostics_and_stops_after_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls      = 0
    diagnostic = "fatal: unable to write new index file " + "x" * 3_000

    def fake_run(
        _command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess([], 128, "", diagnostic)

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    monkeypatch.setattr(git_helper.time, "sleep", lambda _delay: None)

    with pytest.raises(GitCommandError) as exc_info:
        run_git(tmp_path, "add", "-A")

    assert calls == len(git_helper._TRANSIENT_RETRY_DELAYS) + 1
    message = str(exc_info.value)
    assert len(message) <= git_helper._MAX_DIAGNOSTIC_CHARS
    assert "exit code 128" in message
    assert f"repo={tmp_path}" in message
    assert "fatal: unable to write new index file" in message
