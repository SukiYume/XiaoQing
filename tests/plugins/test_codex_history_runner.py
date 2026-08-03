"""历史、归档、runner 和状态恢复。"""

from __future__ import annotations

import tests.helpers.codex_test_support as _fixture_support
from tests.helpers.codex_test_support import (
    PNG_BYTES,
    Any,
    CallbackStreamingProcess,
    CodexRunner,
    FakeContext,
    FakeRunner,
    Path,
    SimpleNamespace,
    _arxiv_addon,
    _install_fake_manager,
    _persisted_session,
    _valid_arxiv_summary,
    _wait_until,
    asyncio,
    codex_main,
    json,
    load_plugin_config,
    os,
    pytest,
    time,
)

reset_codex_manager = _fixture_support.reset_codex_manager


@pytest.mark.asyncio
async def test_arxiv_summary_duplicate_inflight_reports_status(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(block_summary=True)
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-20",
        links=["https://arxiv.org/abs/2605.00001"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await _wait_until(lambda: runner.started == ["astro-ph", "astro-ph"])
    context.actions.clear()

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-20",
        links=["https://arxiv.org/abs/2605.00001"],
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "已在队列或运行中" in result
    assert "已在运行中" in str(context.actions)
    runner.release.set()
    await manager.wait_idle()


@pytest.mark.asyncio
async def test_arxiv_summary_failed_history_is_retried(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(result_text="network failed", exit_code=1)
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-21",
        links=["https://arxiv.org/abs/2605.18513"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()
    assert "2026-05-21 arXiv 总结失败" in str(context.actions)

    retry_runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-21",
            "https://arxiv.org/abs/2605.18513",
            "retry summary",
        )
    )
    manager.runner = retry_runner  # type: ignore[assignment]
    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-21",
        links=["https://arxiv.org/abs/2605.18513"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert len(retry_runner.calls) == 1
    assert "retry summary" in str(context.actions)


@pytest.mark.asyncio
async def test_arxiv_summary_queue_full_reuses_manager_limit(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.config["plugins"]["codex"]["per_session_queue_limit"] = 1
    runner = FakeRunner(block_summary=True)
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-22",
        links=["https://arxiv.org/abs/2605.00001"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await _wait_until(lambda: runner.started == ["astro-ph", "astro-ph"])

    queued = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-23",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    full = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-24",
        links=["https://arxiv.org/abs/2605.18050"],
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "已投递" in queued
    assert "已达到队列上限 1" in full
    runner.release.set()
    await manager.wait_idle()


@pytest.mark.asyncio
async def test_protected_astro_ph_requires_explicit_protected_delete(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create astro-ph", {"user_id": 1, "group_id": 2}, context)

    denied = await codex_main.handle(
        "codex",
        "delete astro-ph --force",
        {"user_id": 1, "group_id": 2},
        context,
    )
    allowed = await codex_main.handle(
        "codex",
        "delete astro-ph --force --protected",
        {"user_id": 1, "group_id": 2},
        context,
    )

    assert "受保护" in str(denied)
    assert "已删除" in str(allowed)
    assert "历史已归档" in str(allowed)
    assert str(context.data_dir) not in str(allowed)
    assert not (context.data_dir / "session" / "astro-ph").exists()
    archives = list((context.data_dir / "deleted_sessions").glob("astro-ph-*"))
    assert len(archives) == 1
    archived_history = (archives[0] / "conversation.jsonl").read_text(encoding="utf-8")
    assert "session.created" in archived_history
    assert "session.deleted" in archived_history


@pytest.mark.asyncio
async def test_codex_session_views_publish_only_cwd_name(tmp_path: Path):
    context = FakeContext(tmp_path)
    manager = _install_fake_manager(context, FakeRunner())
    await manager.create_session("aaa", None, user_id=1, group_id=None)

    listing = await manager.list_sessions()
    status = await manager.status("aaa")

    assert str(context.default_cwd) not in listing
    assert str(context.default_cwd) not in status
    assert "cwd=default-cwd" in listing
    assert "cwd=default-cwd" in status


@pytest.mark.asyncio
async def test_delete_archives_history_and_recreate_uses_clean_session(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner()
    _install_fake_manager(context, runner)

    await codex_main.handle("codex", "create aaa", {"user_id": 1, "group_id": 2}, context)
    deleted = await codex_main.handle(
        "codex", "delete aaa --force", {"user_id": 1, "group_id": 2}, context
    )
    recreated = await codex_main.handle(
        "codex", "create aaa", {"user_id": 1, "group_id": 2}, context
    )

    assert "已删除" in str(deleted)
    assert "已创建" in str(recreated)
    active_history = (context.data_dir / "session" / "aaa" / "conversation.jsonl").read_text(
        encoding="utf-8"
    )
    assert "session.created" in active_history
    assert "session.deleted" not in active_history
    archives = list((context.data_dir / "deleted_sessions").glob("aaa-*"))
    assert len(archives) == 1
    archived_history = (archives[0] / "conversation.jsonl").read_text(encoding="utf-8")
    assert "session.deleted" in archived_history


@pytest.mark.asyncio
async def test_recreated_astro_ph_does_not_replay_archived_summary(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "old summary",
        )
    )
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()
    context.actions.clear()

    await codex_main.handle(
        "codex",
        "delete astro-ph --force --protected",
        {"user_id": 1, "group_id": 2},
        context,
    )

    retry_runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "new summary",
        )
    )
    manager.runner = retry_runner  # type: ignore[assignment]
    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert "已投递" in result
    assert len(retry_runner.calls) == 2
    sent_text = str(context.actions)
    assert "new summary" in sent_text
    assert "old summary" not in sent_text


def test_runner_injects_default_image_artifact_prompt(tmp_path: Path):
    context = FakeContext(tmp_path)
    config = load_plugin_config(context)
    runner = CodexRunner(config, tmp_path / "outputs")
    artifact_dir = tmp_path / "session" / "aaa" / "jobs" / "job-0001" / "artifacts"

    args = runner._build_args(  # noqa: SLF001 - regression coverage for CLI prompt transport.
        tmp_path,
        None,
        tmp_path / "out.txt",
    )
    prompt = runner._prompt_with_artifact_instruction(  # noqa: SLF001
        "画一张图",
        artifact_dir,
    )

    assert args[-1] == "-"
    assert "画一张图" not in args
    assert prompt.startswith("画一张图")
    assert "Codex 插件默认图片输出约定" in prompt
    assert "generated_images" not in prompt
    assert "复制到上述目录" in prompt
    assert artifact_dir.resolve().as_posix() in prompt


@pytest.mark.asyncio
async def test_runner_sends_multiline_prompt_via_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    config = load_plugin_config(context)
    runner = CodexRunner(config, tmp_path / "outputs")
    captured: dict[str, Any] = {}

    async def exchange(payload: bytes) -> tuple[bytes, bytes]:
        captured["stdin"] = payload.decode("utf-8")
        output_path = Path(captured["args"][captured["args"].index("-o") + 1])
        output_path.write_text("## 2026-05-19\nsummary", encoding="utf-8")
        stdout = b'{"type":"thread.started","thread_id":"thread-1"}\n'
        return stdout, b""

    process = CallbackStreamingProcess(exchange, returncode=0)

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["stdin_pipe"] = kwargs.get("stdin")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    prompt = "## 2026-05-19\nhttps://arxiv.org/abs/2605.16917\nhttps://arxiv.org/abs/2605.18050"
    result = await runner.run(
        cwd=tmp_path,
        prompt=prompt,
        thread_id="thread-old",
        job=SimpleNamespace(label="astro-ph", cancel_requested=False),
        artifact_dir=None,
    )

    assert captured["args"][-1] == "-"
    assert prompt not in captured["args"]
    assert captured["stdin_pipe"] == asyncio.subprocess.PIPE
    assert captured["stdin"].startswith(prompt)
    assert "https://arxiv.org/abs/2605.18050" in captured["stdin"]
    assert result.final_text == "## 2026-05-19\nsummary"


def test_session_state_recovers_truncated_primary_from_valid_backup(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    context.data_dir.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "sessions": {"safe": _persisted_session("safe", context.default_cwd)},
    }
    sessions_path = context.data_dir / "sessions.json"
    sessions_path.write_text('{"schema_version": 1,', encoding="utf-8")
    sessions_path.with_name("sessions.json.bak").write_text(
        json.dumps(state),
        encoding="utf-8",
    )

    manager = _install_fake_manager(context, FakeRunner())

    assert set(manager.sessions) == {"safe"}
    assert json.loads(sessions_path.read_text(encoding="utf-8")) == state
    assert (
        json.loads(sessions_path.with_name("sessions.json.bak").read_text(encoding="utf-8"))
        == state
    )
    assert len(list((context.data_dir / "quarantine").glob("sessions-*.json"))) == 1


def test_session_state_keeps_valid_records_and_quarantines_invalid_fields(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    context.data_dir.mkdir(parents=True)
    invalid = _persisted_session("invalid", context.default_cwd)
    invalid["unexpected"] = "must not be accepted"
    (context.data_dir / "sessions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sessions": {
                    "valid": _persisted_session("valid", context.default_cwd),
                    "invalid": invalid,
                },
            }
        ),
        encoding="utf-8",
    )

    manager = _install_fake_manager(context, FakeRunner())
    cleaned = json.loads((context.data_dir / "sessions.json").read_text(encoding="utf-8"))

    assert set(manager.sessions) == {"valid"}
    assert cleaned["schema_version"] == 1
    assert set(cleaned["sessions"]) == {"valid"}
    assert len(list((context.data_dir / "quarantine").glob("sessions-*.json"))) == 1


def test_session_state_rejects_future_schema_without_blocking_startup(tmp_path: Path):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    context.data_dir.mkdir(parents=True)
    (context.data_dir / "sessions.json").write_text(
        json.dumps({"schema_version": 999, "sessions": {}}),
        encoding="utf-8",
    )

    manager = _install_fake_manager(context, FakeRunner())
    cleaned = json.loads((context.data_dir / "sessions.json").read_text(encoding="utf-8"))

    assert manager.sessions == {}
    assert cleaned == {"schema_version": 1, "sessions": {}}
    assert len(list((context.data_dir / "quarantine").glob("sessions-*.json"))) == 1


def test_codex_queue_limits_are_bounded_and_emergency_limit_cannot_undercut_them(
    tmp_path: Path,
):
    context = FakeContext(tmp_path)
    raw = context.config["plugins"]["codex"]
    raw["per_session_queue_limit"] = 99_999
    raw["emergency_queue_limit"] = 10

    config = load_plugin_config(context)

    assert config.per_session_queue_limit == 1_000
    assert config.emergency_queue_limit == 1_000


@pytest.mark.asyncio
async def test_maintenance_prunes_orphans_and_expires_only_idle_unprotected_sessions(
    tmp_path: Path,
):
    context = FakeContext(tmp_path)
    context.default_cwd.mkdir(parents=True)
    raw = context.config["plugins"]["codex"]
    raw["artifact_retention_days"] = 1
    raw["session_ttl_days"] = 1
    manager = _install_fake_manager(context, FakeRunner())
    await manager.create_session(
        "expired",
        str(context.default_cwd),
        user_id=1,
        group_id=2,
    )
    await manager.create_session(
        "astro-ph",
        str(context.default_cwd),
        user_id=1,
        group_id=2,
    )
    old = time.time() - 3 * 86400
    manager.sessions["expired"].updated_at = old
    manager.sessions["astro-ph"].updated_at = old
    manager._save()  # noqa: SLF001 - persisted lifecycle regression setup.

    orphan_job = manager.session_root / "astro-ph" / "jobs" / "job-9999"
    (orphan_job / "artifacts").mkdir(parents=True)
    (orphan_job / "artifacts" / "old.png").write_bytes(PNG_BYTES)
    active_job = manager.session_root / "active" / "jobs" / "job-0001"
    (active_job / "artifacts").mkdir(parents=True)
    (active_job / "artifacts" / "active.png").write_bytes(PNG_BYTES)
    manager.output_dir.mkdir(parents=True)
    orphan_output = manager.output_dir / "codex-orphan.txt"
    orphan_output.write_text("orphan", encoding="utf-8")
    quarantine = manager.data_dir / "quarantine" / "old.json"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    quarantine.write_text("{}", encoding="utf-8")
    deleted = manager.deleted_session_root / "old-deleted"
    deleted.mkdir(parents=True)
    for path in (orphan_job, active_job, orphan_output, quarantine, deleted):
        os.utime(path, (old, old))
    manager.running["active"] = SimpleNamespace(label="active", job_id=1)

    await manager.maintenance()

    assert not orphan_job.exists()
    assert active_job.exists()
    assert orphan_output.exists()
    assert not quarantine.exists()
    assert not deleted.exists()
    assert "expired" not in manager.sessions
    assert "astro-ph" in manager.sessions
    assert list(manager.deleted_session_root.glob("expired-*"))

    manager.running.clear()
    await manager.maintenance()

    assert not active_job.exists()
    assert not orphan_output.exists()
