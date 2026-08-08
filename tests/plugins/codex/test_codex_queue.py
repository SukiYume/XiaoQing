"""Codex 清单、队列、取消和结果投递。"""

from __future__ import annotations

import threading

import tests.helpers.codex_test_support as _fixture_support
from tests.helpers.codex_test_support import (
    MAX_MESSAGE_TEXT_LENGTH,
    PNG_BYTES,
    AsyncMock,
    CodexImageArtifact,
    CwdError,
    FakeContext,
    FakeRunner,
    Path,
    _arxiv_addon,
    _install_actual_runner_manager,
    _install_fake_manager,
    _patch_race_termination,
    _RaceProcess,
    _valid_arxiv_summary,
    _wait_manager_idle,
    _wait_until,
    asyncio,
    codex_main,
    json,
    load_plugin_config,
    normalize_cwd,
    pytest,
)
from tests.helpers.paths import REPOSITORY_ROOT

reset_codex_manager = _fixture_support.reset_codex_manager


def test_codex_manifest_restricts_every_command_to_admins() -> None:
    manifest_path = REPOSITORY_ROOT / "plugins" / "codex" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    commands = manifest.get("commands")
    assert commands, "Codex manifest must declare at least one command"
    assert all(command.get("admin_only") is True for command in commands)


def test_codex_config_rejects_implicit_coercions_and_clamps_resources(tmp_path: Path) -> None:
    context = FakeContext(tmp_path)
    raw = context.config["plugins"]["codex"]
    raw.update(
        {
            "allowed_cwd_roots": [str(tmp_path), 123, "", str(tmp_path)],
            "max_parallel_jobs": True,
            "max_prompt_chars": 9_000_000,
            "spawn_timeout_seconds": 1.5,
            "job_timeout_seconds": "90",
            "max_image_bytes": 100 * 1024**2,
            "max_image_total_bytes": 64 * 1024,
            "sandbox": "escape-everything",
            "approval_policy": ["never"],
            "skip_git_repo_check": "false",
            "protected_sessions": ["safe", 7, "bad\nlabel"],
            "arxiv_summary": {"label": "bad label", "methodology": []},
            "session_ttl_days": True,
            "artifact_retention_days": 99_999,
            "emergency_disk_bytes": -1,
        }
    )

    config = load_plugin_config(context)

    assert config.allowed_cwd_roots == (str(tmp_path),)
    assert config.max_parallel_jobs == 2
    assert config.max_prompt_chars == 1_000_000
    assert config.spawn_timeout_seconds == 30
    assert config.job_timeout_seconds == 3_600
    assert config.max_image_total_bytes == config.max_image_bytes == 100 * 1024**2
    assert config.sandbox == "workspace-write"
    assert config.approval_policy == "never"
    assert config.skip_git_repo_check is True
    assert config.protected_sessions == ("astro-ph", "safe")
    assert config.session_ttl_days == 90
    assert config.artifact_retention_days == 3_650
    assert config.emergency_disk_bytes == 64 * 1024**2


def test_codex_unconfigured_workspace_defaults_to_plugin_data_dir(tmp_path: Path) -> None:
    context = FakeContext(tmp_path)
    context.config = {"plugins": {"codex": {}}}

    config = load_plugin_config(context)

    expected = str((context.data_dir / "workspaces").resolve(strict=False))
    assert config.default_cwd == expected
    assert config.allowed_cwd_roots == (expected,)
    assert config.arxiv_summary_cwd == expected


def test_danger_full_access_emits_a_startup_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["sandbox"] = "danger-full-access"

    with caplog.at_level("WARNING", logger="plugins.codex.manager"):
        _install_fake_manager(context, FakeRunner())

    assert "sandbox=danger-full-access" in caplog.text


def test_default_cwd_is_not_created_before_allowed_root_validation(tmp_path: Path) -> None:
    context = FakeContext(tmp_path)
    outside = tmp_path / "outside" / "new-default"
    safe_root = tmp_path / "safe"
    raw = context.config["plugins"]["codex"]
    raw["default_cwd"] = str(outside)
    raw["allowed_cwd_roots"] = [str(safe_root)]

    with pytest.raises(CwdError, match="不在允许范围"):
        normalize_cwd(None, load_plugin_config(context))

    assert not outside.exists()


@pytest.mark.asyncio
async def test_help_does_not_construct_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_manager = AsyncMock(side_effect=AssertionError("manager must not be constructed"))
    monkeypatch.setattr(codex_main, "get_manager", get_manager)

    result = await codex_main.handle("codex", "help", {}, FakeContext(tmp_path))

    assert "Codex 会话队列" in str(result)
    get_manager.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_session_uses_default_cwd_and_creates_directory(tmp_path: Path):
    from core.config import ConfigSnapshot

    context = FakeContext(tmp_path)
    snapshot = ConfigSnapshot(
        config=context.config,
        secrets={"plugins": {"codex": {"max_parallel_jobs": 7, "job_timeout_seconds": 777}}},
    )
    context.config = snapshot.config
    context.secrets = snapshot.secrets
    loaded = load_plugin_config(context)
    assert loaded.max_parallel_jobs == 7
    assert loaded.job_timeout_seconds == 777
    runner = FakeRunner()
    _install_fake_manager(context, runner)

    result = await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)

    assert context.default_cwd.exists()
    assert "已创建" in str(result)
    assert "aaa" in str(result)


@pytest.mark.asyncio
async def test_create_session_rejects_cwd_outside_allowed_roots(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)
    outside = tmp_path.parent / "outside-codex-test"
    outside.mkdir(exist_ok=True)

    result = await codex_main.handle(
        "codex", f"create aaa cwd:{outside.as_posix()}", {"user_id": 1}, context
    )

    assert "不在允许范围" in str(result)


@pytest.mark.asyncio
async def test_command_parser_rejects_unknown_flags_values_and_ambiguous_ids(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.current_user_id = 42
    context.current_group_id = 43
    manager = _install_fake_manager(context, FakeRunner())

    await codex_main.handle(
        "codex",
        "create aaa",
        {"user_id": True, "group_id": 0},
        context,
    )
    unknown = await codex_main.handle("codex", "delete aaa --forceful", {}, context)
    valued = await codex_main.handle("codex", "delete aaa --force=maybe", {}, context)
    bad_id = await codex_main.handle("codex", "cancel aaa +1", {}, context)
    extra = await codex_main.handle("codex", "list extra", {}, context)

    assert manager.sessions["aaa"].owner_user_id == 42
    assert manager.sessions["aaa"].target_group_id == 43
    assert "不支持的选项" in str(unknown)
    assert "无值标志" in str(valued)
    assert "ASCII 十进制整数" in str(bad_id)
    assert "参数数量不正确" in str(extra)
    assert "aaa" in manager.sessions


@pytest.mark.asyncio
async def test_prompt_limit_is_enforced_at_internal_queue_boundary(tmp_path: Path) -> None:
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["max_prompt_chars"] = 1_000
    manager = _install_fake_manager(context, FakeRunner())
    await manager.create_session("aaa", None, user_id=1, group_id=None)

    message = await manager.enqueue(
        "aaa",
        "x" * 1_001,
        user_id=1,
        group_id=None,
        context=context,
    )

    assert "超过 1000 字符上限" in message
    assert not manager.queues.get("aaa")


@pytest.mark.asyncio
async def test_enqueue_measures_disk_outside_event_loop_and_manager_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext(tmp_path)
    manager = _install_fake_manager(context, FakeRunner())
    await manager.create_session("aaa", None, user_id=1, group_id=None)
    event_loop_thread = threading.get_ident()
    observations: list[tuple[int, bool]] = []

    def measure_disk(**_kwargs: object) -> int:
        observations.append((threading.get_ident(), manager.lock.locked()))
        return 0

    monkeypatch.setattr(manager, "_disk_usage_bytes", measure_disk)
    await manager.enqueue("aaa", "first", user_id=1, group_id=None, context=context)
    await _wait_manager_idle(manager)

    assert len(observations) == 1
    assert observations[0][0] != event_loop_thread
    assert observations[0][1] is False


@pytest.mark.asyncio
async def test_enqueue_history_failure_rolls_back_without_orphan_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)
    await manager.create_session("aaa", None, user_id=1, group_id=None)
    original_append_history = manager._append_history

    def fail_user_history(label: str, payload: dict[str, object]) -> None:
        if payload.get("role") == "user":
            raise OSError("private history path")
        original_append_history(label, payload)

    monkeypatch.setattr(manager, "_append_history", fail_user_history)
    response = await manager.enqueue(
        "aaa",
        "must not run",
        user_id=1,
        group_id=None,
        context=context,
    )

    assert "未加入执行队列" in response
    assert manager.sessions["aaa"].total_jobs == 0
    assert not manager.queues.get("aaa")
    assert manager.running == {}
    assert manager.workers == {}

    monkeypatch.setattr(manager, "_append_history", original_append_history)
    await manager.enqueue("aaa", "second", user_id=1, group_id=None, context=context)
    await _wait_manager_idle(manager)

    assert [prompt for _, prompt, _ in runner.calls] == ["second"]
    assert manager.sessions["aaa"].total_jobs == 1


@pytest.mark.asyncio
async def test_enqueue_state_save_failure_never_publishes_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext(tmp_path)
    manager = _install_fake_manager(context, FakeRunner())
    await manager.create_session("aaa", None, user_id=1, group_id=None)
    original_save = manager._save
    monkeypatch.setattr(
        manager,
        "_save",
        lambda: (_ for _ in ()).throw(OSError("private state path")),
    )

    response = await manager.enqueue(
        "aaa",
        "must not run",
        user_id=1,
        group_id=None,
        context=context,
    )
    monkeypatch.setattr(manager, "_save", original_save)

    assert "未加入执行队列" in response
    assert manager.sessions["aaa"].total_jobs == 0
    assert not manager.queues.get("aaa")
    assert manager.running == {}
    assert manager.workers == {}


@pytest.mark.asyncio
async def test_same_label_queue_runs_serially_and_sends_results(tmp_path: Path):
    context = FakeContext(tmp_path, max_parallel_jobs=2)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    first = await codex_main.handle(
        "codex", "aaa block first", {"user_id": 1, "group_id": 2}, context
    )
    second = await codex_main.handle("codex", "aaa second", {"user_id": 1, "group_id": 2}, context)

    await _wait_until(lambda: runner.started == ["aaa"])
    assert "开始后台执行" in str(first)
    assert "前面还有 1 个任务" in str(second)

    runner.release.set()
    await _wait_manager_idle(manager)

    assert [call[1] for call in runner.calls] == ["block first", "second"]
    sent_text = str(context.actions)
    assert "[codex:aaa #1] 完成" in sent_text
    assert "[codex:aaa #2] 完成" in sent_text


@pytest.mark.asyncio
async def test_private_job_result_stays_private_when_session_created_in_group(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="private reply")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle(
        "codex", "aaa hello", {"message_type": "private", "user_id": 1}, context
    )
    await _wait_manager_idle(manager)

    assert len(context.actions) == 1
    action = context.actions[0]
    assert action["action"] == "send_private_msg"
    assert action["params"]["user_id"] == 1
    assert "group_id" not in action["params"]
    assert "private reply" in str(action["params"]["message"])


@pytest.mark.asyncio
async def test_different_labels_can_run_in_parallel(tmp_path: Path):
    context = FakeContext(tmp_path, max_parallel_jobs=2)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "create bbb", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa block one", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "bbb block two", {"user_id": 1, "group_id": 2}, context)

    await _wait_until(lambda: runner.started.count("aaa") == 1 and runner.started.count("bbb") == 1)
    assert set(runner.started) == {"aaa", "bbb"}

    runner.release.set()
    await _wait_manager_idle(manager)
    assert len(context.actions) == 2


@pytest.mark.asyncio
async def test_refresh_failure_after_permit_acquire_releases_capacity(tmp_path: Path) -> None:
    context = FakeContext(tmp_path, max_parallel_jobs=1)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)
    await manager.create_session("aaa", None, user_id=1, group_id=None)
    refresh_calls = 0

    async def refresh() -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            raise RuntimeError("private refresh detail")
        return False

    manager.refresh_from_settings_reader = refresh  # type: ignore[method-assign]
    await manager.enqueue("aaa", "first", user_id=1, group_id=None, context=context)
    await _wait_manager_idle(manager)

    assert not manager.global_sem.locked()
    assert "private refresh detail" not in str(context.actions)

    await manager.enqueue("aaa", "second", user_id=1, group_id=None, context=context)
    await _wait_manager_idle(manager)

    assert runner.started == ["aaa"]


@pytest.mark.asyncio
async def test_artifact_directory_failure_cannot_leave_claimed_job_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)
    await manager.create_session("aaa", None, user_id=1, group_id=None)
    monkeypatch.setattr(
        manager,
        "_job_artifact_dir",
        lambda _label, _job_id: (_ for _ in ()).throw(OSError("private path detail")),
    )

    await manager.enqueue("aaa", "first", user_id=1, group_id=None, context=context)
    job = manager.queues["aaa"][0]
    await _wait_manager_idle(manager)

    assert manager.running == {}
    assert job.status == "failed"
    assert job.result is not None and "OSError" in job.result.final_text
    assert "private path detail" not in job.result.final_text
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_delivery_failure_does_not_stop_same_label_queue(tmp_path: Path) -> None:
    context = FakeContext(tmp_path)
    context.send_action = AsyncMock(side_effect=[OSError("private delivery detail"), None])
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)
    await manager.create_session("aaa", None, user_id=1, group_id=None)

    await manager.enqueue("aaa", "first", user_id=1, group_id=None, context=context)
    await manager.enqueue("aaa", "second", user_id=1, group_id=None, context=context)
    await _wait_manager_idle(manager)

    assert [prompt for _, prompt, _ in runner.calls] == ["first", "second"]
    assert context.send_action.await_count == 2


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_global_slot_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    for _ in range(manager.config.max_parallel_jobs):
        await manager.global_sem.acquire()
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    try:
        await manager.enqueue(
            "race",
            "must not spawn",
            user_id=1,
            group_id=2,
            context=context,
        )
        await _wait_until(lambda: "race" in manager.running)
        job = manager.running["race"]

        response = await asyncio.wait_for(manager.cancel("race", job.job_id), timeout=2)
        await _wait_manager_idle(manager)
    finally:
        for _ in range(manager.config.max_parallel_jobs):
            manager.global_sem.release()

    assert "已取消" in response
    spawn.assert_not_awaited()
    assert job.status == "cancelled"
    assert job.prompt_started is False
    assert job.finished_event.is_set()
    assert manager.running == {}


@pytest.mark.asyncio
async def test_cancel_during_spawn_waits_for_handoff_and_never_sends_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    terminate = _patch_race_termination(monkeypatch)

    await manager.enqueue("race", "side effect", user_id=1, group_id=2, context=context)
    await spawn_started.wait()
    job = manager.running["race"]
    assert job.status == "starting"
    cancel_task = asyncio.create_task(manager.cancel("race", job.job_id))
    await asyncio.sleep(0)
    assert cancel_task.done() is False

    allow_spawn.set()
    response = await asyncio.wait_for(cancel_task, timeout=2)
    await _wait_manager_idle(manager)

    assert "已取消" in response
    assert not any(process.stdin_inputs)
    assert process.prompt_sent.is_set() is False
    assert job.prompt_started is False
    assert job.status == "cancelled"
    assert job.result is not None and job.result.cancelled is True
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert terminate.await_count >= 1
    history = [
        json.loads(line)
        for line in (context.data_dir / "session" / "race" / "conversation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assistant_events = [
        event
        for event in history
        if event.get("role") == "assistant" and event.get("job_id") == job.job_id
    ]
    assert len(assistant_events) == 1
    assert assistant_events[0]["status"] == "cancelled"
    assert assistant_events[0]["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_after_process_registration_but_before_prompt_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    before_prompt = asyncio.Event()
    allow_prompt = asyncio.Event()
    original_authorize = manager._authorize_job_prompt

    async def delayed_authorize(label, job):
        before_prompt.set()
        await allow_prompt.wait()
        return await original_authorize(label, job)

    manager._authorize_job_prompt = delayed_authorize  # type: ignore[method-assign]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    terminate = _patch_race_termination(monkeypatch)

    await manager.enqueue("race", "side effect", user_id=1, group_id=2, context=context)
    await before_prompt.wait()
    job = manager.running["race"]
    assert job.spawn_handoff.is_set()
    assert job.process is process
    assert job.prompt_started is False

    cancel_task = asyncio.create_task(manager.cancel("race", job.job_id))
    await _wait_until(lambda: job.cancel_requested)
    allow_prompt.set()
    response = await asyncio.wait_for(cancel_task, timeout=2)
    await _wait_manager_idle(manager)

    assert "已取消" in response
    assert not any(process.stdin_inputs)
    assert process.prompt_sent.is_set() is False
    assert job.status == "cancelled"
    assert job.result is not None and job.result.cancelled is True
    assert terminate.await_count >= 1


@pytest.mark.asyncio
async def test_cancel_after_prompt_commit_terminates_and_finishes_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    _patch_race_termination(monkeypatch)

    await manager.enqueue("race", "running body", user_id=1, group_id=2, context=context)
    await process.prompt_sent.wait()
    job = manager.running["race"]
    assert job.status == "running"
    assert job.prompt_started is True

    response = await asyncio.wait_for(manager.cancel("race", job.job_id), timeout=2)
    await _wait_manager_idle(manager)

    assert "已取消" in response
    assert len([payload for payload in process.stdin_inputs if payload]) == 1
    assert job.status == "cancelled"
    assert job.result is not None and job.result.cancelled is True
    assert manager.running == {}


@pytest.mark.asyncio
async def test_spawn_failure_completes_handoff_and_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    spawn_started = asyncio.Event()
    fail_spawn = asyncio.Event()

    async def failing_spawn(*_args, **_kwargs):
        spawn_started.set()
        await fail_spawn.wait()
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_spawn)
    await manager.enqueue("race", "body", user_id=1, group_id=2, context=context)
    await spawn_started.wait()
    job = manager.running["race"]
    fail_spawn.set()
    await _wait_manager_idle(manager)

    assert job.status == "failed"
    assert job.result is not None and "OSError" in job.result.final_text
    assert "spawn failed" not in job.result.final_text
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert job.process is None
    assert not list((context.data_dir / "outputs").glob("codex-last-*.txt"))


@pytest.mark.asyncio
async def test_shutdown_during_spawn_uses_same_handoff_without_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    process = _RaceProcess()
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()

    async def delayed_spawn(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    _patch_race_termination(monkeypatch)
    await manager.enqueue("race", "shutdown body", user_id=1, group_id=2, context=context)
    await spawn_started.wait()
    job = manager.running["race"]

    shutdown_task = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    assert shutdown_task.done() is False
    allow_spawn.set()
    await asyncio.wait_for(shutdown_task, timeout=2)

    assert not any(process.stdin_inputs)
    assert process.prompt_sent.is_set() is False
    assert job.status == "cancelled"
    assert job.finished_event.is_set()
    assert manager.running == {}
    assert manager.workers == {}


@pytest.mark.asyncio
async def test_spawn_timeout_is_bounded_and_finalizes_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["spawn_timeout_seconds"] = 1
    context.default_cwd.mkdir(parents=True)
    manager = _install_actual_runner_manager(context)
    await manager.create_session("race", None, user_id=1, group_id=2)
    never = asyncio.Event()

    async def stuck_spawn(*_args, **_kwargs):
        await never.wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stuck_spawn)
    await manager.enqueue("race", "body", user_id=1, group_id=2, context=context)
    await _wait_until(lambda: "race" in manager.running)
    job = manager.running["race"]

    await asyncio.wait_for(_wait_manager_idle(manager), timeout=2)

    assert job.status == "failed"
    assert job.result is not None and "RuntimeError" in job.result.final_text
    assert "spawn timed out" not in job.result.final_text
    assert job.spawn_handoff.is_set() and job.finished_event.is_set()
    assert manager.running == {}


@pytest.mark.asyncio
async def test_result_is_not_truncated_by_plugin(tmp_path: Path):
    context = FakeContext(tmp_path)
    long_text = "x" * 5000
    runner = FakeRunner(result_text=long_text)
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa long answer", {"user_id": 1, "group_id": 2}, context)
    await _wait_manager_idle(manager)

    sent_text = "".join(
        seg["data"]["text"]
        for action in context.actions
        for seg in action["params"]["message"]
        if seg.get("type") == "text"
    )
    assert long_text in sent_text
    assert "已截断" not in sent_text


@pytest.mark.asyncio
async def test_conversation_history_is_saved_per_session(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="assistant reply")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa user prompt", {"user_id": 1, "group_id": 2}, context)
    await _wait_manager_idle(manager)

    history_path = context.data_dir / "session" / "aaa" / "conversation.jsonl"
    events = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]

    assert events[0]["type"] == "session.created"
    assert any(
        event.get("role") == "user" and event.get("content") == "user prompt" for event in events
    )
    assert any(
        event.get("role") == "assistant" and event.get("content") == "assistant reply"
        for event in events
    )


@pytest.mark.asyncio
async def test_markdown_image_result_is_copied_and_sent(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    image_path = context.default_cwd / "plot.png"
    image_path.write_bytes(PNG_BYTES)
    runner = FakeRunner(result_text=f"生成完成\n![plot]({image_path.as_posix()})")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa draw a plot", {"user_id": 1, "group_id": 2}, context)
    await _wait_manager_idle(manager)

    message = context.actions[-1]["params"]["message"]
    image_segments = [seg for seg in message if seg.get("type") == "image"]
    assert image_segments
    copied = context.data_dir / "session" / "aaa" / "images" / "job-0001-01.png"
    assert copied.exists()
    assert image_segments[0]["data"]["file"] == copied.resolve().as_uri()

    history_path = context.data_dir / "session" / "aaa" / "conversation.jsonl"
    events = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()]
    assistant_event = [event for event in events if event.get("role") == "assistant"][-1]
    assert assistant_event["images"][0]["path"] == "images/job-0001-01.png"


@pytest.mark.asyncio
async def test_artifact_directory_images_are_sent_without_text_marker(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="生成完成", artifact_name="chart.png")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa draw a chart", {"user_id": 1, "group_id": 2}, context)
    await _wait_manager_idle(manager)

    message = context.actions[-1]["params"]["message"]
    assert any(seg.get("type") == "image" for seg in message)
    assert (context.data_dir / "session" / "aaa" / "images" / "job-0001-01.png").exists()
    assert not (context.data_dir / "session" / "aaa" / "jobs" / "job-0001" / "artifacts").exists()


def test_result_message_batches_enforce_qq_image_limit(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["max_qq_images"] = 1
    manager = _install_fake_manager(context, FakeRunner())
    artifacts = [
        CodexImageArtifact(
            path=f"images/{index}.png",
            absolute_path=str(tmp_path / f"{index}.png"),
            source="artifact",
            original_path=f"{index}.png",
        )
        for index in range(3)
    ]

    batches = manager._result_message_batches("done", artifacts)  # noqa: SLF001

    assert sum(segment.get("type") == "image" for batch in batches for segment in batch) == 1


@pytest.mark.asyncio
async def test_generated_images_dir_is_not_guessed_without_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="已生成。", generated_image_name="cyberpunk.png")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create img", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "img $imagegen draw", {"user_id": 1, "group_id": 2}, context)
    await _wait_manager_idle(manager)

    message = context.actions[-1]["params"]["message"]
    assert not any(seg.get("type") == "image" for seg in message)
    copied = context.data_dir / "session" / "img" / "images" / "job-0001-01.png"
    assert not copied.exists()


@pytest.mark.asyncio
async def test_long_text_with_image_is_split_before_image(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    image_path = context.default_cwd / "plot.png"
    image_path.write_bytes(PNG_BYTES)
    long_text = "x" * (MAX_MESSAGE_TEXT_LENGTH + 10)
    runner = FakeRunner(result_text=f"{long_text}\n图片: {image_path.as_posix()}")
    manager = _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    await codex_main.handle("codex", "aaa draw long report", {"user_id": 1, "group_id": 2}, context)
    await _wait_manager_idle(manager)

    assert len(context.actions) >= 2
    assert all(seg.get("type") == "text" for seg in context.actions[0]["params"]["message"])
    assert any(seg.get("type") == "image" for seg in context.actions[-1]["params"]["message"])


@pytest.mark.asyncio
async def test_arxiv_summary_auto_creates_astro_ph_and_runs(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
        )
    )
    manager = _install_fake_manager(context, runner)

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await _wait_manager_idle(manager)

    assert "已投递" in result
    assert "astro-ph" in manager.sessions
    assert manager.sessions["astro-ph"].cwd == str(context.default_cwd.resolve(strict=False))
    assert len(runner.calls) == 2
    init_label, init_prompt, init_thread_id = runner.calls[0]
    summary_label, summary_prompt, summary_thread_id = runner.calls[1]
    assert init_label == "astro-ph"
    assert init_thread_id is None
    assert "arxiv-summary-methodology.md" in init_prompt
    assert "这条消息只用于初始化会话规则" in init_prompt
    assert "https://arxiv.org/abs/2605.16917" not in init_prompt
    assert summary_label == "astro-ph"
    assert summary_thread_id == "thread-astro-ph"
    assert "请先读取当前工作目录下的 `arxiv-summary-methodology.md`" in summary_prompt
    assert "不要把方法论文件内容复述出来" in summary_prompt
    assert "## 2026-05-19\nhttps://arxiv.org/abs/2605.16917" in summary_prompt
    sent_text = str(context.actions)
    assert "[codex:astro-ph #1]" not in sent_text
    assert "[codex:astro-ph #2] 完成" in sent_text
    assert "summary" in sent_text
