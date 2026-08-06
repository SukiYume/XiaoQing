"""arXiv 摘要和管理器策略。"""

from __future__ import annotations

import tests.helpers.codex_test_support as _fixture_support
from tests.helpers.codex_test_support import (
    Any,
    AsyncMock,
    CodexQueueManager,
    CodexRunResult,
    DeliveryTarget,
    FakeContext,
    FakeRunner,
    Path,
    PluginCapabilities,
    PluginPrincipal,
    PluginSettingsSnapshot,
    SimpleNamespace,
    _arxiv_addon,
    _install_fake_manager,
    _valid_arxiv_summary,
    _wait_until,
    asyncio,
    codex_arxiv_summary,
    codex_main,
    json,
    pytest,
)

reset_codex_manager = _fixture_support.reset_codex_manager


@pytest.mark.asyncio
async def test_arxiv_summary_validates_date_links_and_canonicalizes_pdf_urls(
    tmp_path: Path,
) -> None:
    context = FakeContext(tmp_path)
    canonical = "https://arxiv.org/abs/2605.16917"
    runner = FakeRunner(result_text=_valid_arxiv_summary("2026-05-19", canonical))
    manager = _install_fake_manager(context, runner)
    addon = _arxiv_addon(manager)

    bad_date = await addon.enqueue_or_replay(
        date="2026-02-30",
        links=[canonical],
        user_id=1,
        group_id=2,
        context=context,
    )
    bad_link = await addon.enqueue_or_replay(
        date="2026-05-19",
        links=["https://example.com/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )
    too_many = await addon.enqueue_or_replay(
        date="2026-05-19",
        links=[canonical] * (codex_arxiv_summary.MAX_ARXIV_LINKS + 1),
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "YYYY-MM-DD" in bad_date
    assert "仅支持 arxiv.org" in bad_link
    assert "最多接受" in too_many
    assert manager.sessions == {}

    queued = await addon.enqueue_or_replay(
        date="2026-05-19",
        links=["http://arxiv.org/pdf/2605.16917v3.pdf?download=1"],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert "已投递" in queued
    assert canonical in runner.calls[-1][1]
    assert "v3.pdf" not in runner.calls[-1][1]


@pytest.mark.asyncio
async def test_arxiv_summary_public_entrypoint_uses_addon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    context.principal = PluginPrincipal(
        kind="scheduled_system",
        delivery_targets=(DeliveryTarget("group", 2),),
    )
    context.capabilities = PluginCapabilities(is_system=True)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "entrypoint summary",
        )
    )
    manager = _install_fake_manager(context, runner)

    async def fake_get_manager(_context: Any) -> CodexQueueManager:
        return manager

    import plugins.codex.manager as manager_module

    monkeypatch.setattr(manager_module, "get_manager", fake_get_manager)
    result = await codex_arxiv_summary.enqueue_or_replay_arxiv_summary(
        context,
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
    )
    await manager.wait_idle()

    assert "已投递" in result
    assert len(runner.calls) == 2
    assert "entrypoint summary" in str(context.actions)


@pytest.mark.asyncio
async def test_system_arxiv_summary_fans_out_once_and_never_uses_old_session_owner(
    tmp_path: Path,
):
    targets = (DeliveryTarget("group", 11), DeliveryTarget("group", 22))
    context = FakeContext(tmp_path)
    context.principal = PluginPrincipal(kind="scheduled_system", delivery_targets=targets)
    context.capabilities = PluginCapabilities(is_system=True)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-07-11",
            "https://arxiv.org/abs/2607.00001",
            "fanout summary",
        )
    )
    manager = _install_fake_manager(context, runner)
    await manager.create_session("astro-ph", None, user_id=999, group_id=333)
    manager.sessions["astro-ph"].thread_id = "existing-thread"

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-07-11",
        links=["https://arxiv.org/abs/2607.00001"],
        user_id=None,
        group_id=None,
        context=context,
        delivery_targets=targets,
    )
    await manager.wait_idle()

    assert "已投递" in result
    assert len(runner.calls) == 1
    assert [action["params"].get("group_id") for action in context.actions] == [11, 22]
    assert all(action["action"] == "send_group_msg" for action in context.actions)

    context.actions.clear()
    replay_targets = (DeliveryTarget("group", 44), DeliveryTarget("group", 55))
    replay = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-07-11",
        links=["https://arxiv.org/abs/2607.00001"],
        user_id=None,
        group_id=None,
        context=context,
        delivery_targets=replay_targets,
    )
    assert "已重发" in replay
    assert len(runner.calls) == 1
    assert [action["params"].get("group_id") for action in context.actions] == [44, 55]

    context.actions.clear()
    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-07-12",
        links=["https://arxiv.org/abs/2607.00002"],
        user_id=None,
        group_id=None,
        context=context,
        delivery_targets=(),
    )
    await manager.wait_idle()
    assert len(runner.calls) == 2
    assert context.actions == []


@pytest.mark.asyncio
async def test_same_date_replays_only_when_canonical_link_set_matches(
    tmp_path: Path,
) -> None:
    context = FakeContext(tmp_path)
    first_link = "https://arxiv.org/abs/2608.00001"
    second_link = "https://arxiv.org/abs/2608.00002"
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-08-06",
            first_link,
            "first listing summary",
        )
    )
    manager = _install_fake_manager(context, runner)

    await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-08-06",
        links=[first_link],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()
    assert len(runner.calls) == 2  # 初始化 + 第一份摘要

    context.actions.clear()
    replay = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-08-06",
        links=["http://arxiv.org/pdf/2608.00001v3.pdf?download=1"],
        user_id=1,
        group_id=2,
        context=context,
    )
    assert "已重发" in replay
    assert len(runner.calls) == 2

    runner.result_text = _valid_arxiv_summary(
        "2026-08-06",
        second_link,
        "updated listing summary",
    )
    context.actions.clear()
    updated = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-08-06",
        links=[second_link],
        user_id=1,
        group_id=2,
        context=context,
    )
    await manager.wait_idle()

    assert "已投递" in updated
    assert len(runner.calls) == 3
    assert second_link in runner.calls[-1][1]
    assert first_link not in runner.calls[-1][1]
    assert "updated listing summary" in str(context.actions)


@pytest.mark.asyncio
async def test_arxiv_summary_public_entrypoint_rejects_unprivileged_or_wrong_context(
    tmp_path: Path,
):
    context = FakeContext(tmp_path)
    context.principal = PluginPrincipal(kind="user", user_id=1, is_private=True)

    with pytest.raises(PermissionError, match="authorization"):
        await codex_arxiv_summary.enqueue_or_replay_arxiv_summary(
            context,
            date="2026-05-19",
            links=["https://arxiv.org/abs/2605.16917"],
            user_id=1,
            group_id=2,
        )

    context.capabilities = PluginCapabilities(is_bot_admin=True)
    context.plugin_name = "arxiv_filter"
    with pytest.raises(PermissionError, match="Codex-scoped"):
        await codex_arxiv_summary.enqueue_or_replay_arxiv_summary(
            context,
            date="2026-05-19",
            links=["https://arxiv.org/abs/2605.16917"],
            user_id=1,
            group_id=2,
        )


@pytest.mark.asyncio
async def test_codex_main_exports_only_the_fixed_arxiv_summary_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context = FakeContext(tmp_path)
    exported = AsyncMock(return_value="queued")
    monkeypatch.setattr(codex_main, "enqueue_or_replay_arxiv_summary", exported)

    assert (
        await codex_main.enqueue_arxiv_summary(
            context,
            date="2026-07-11",
            links=["https://arxiv.org/abs/2607.00001"],
            user_id=1,
            group_id=2,
        )
        == "queued"
    )
    exported.assert_awaited_once_with(
        context,
        date="2026-07-11",
        links=["https://arxiv.org/abs/2607.00001"],
        user_id=1,
        group_id=2,
    )


@pytest.mark.asyncio
async def test_codex_shutdown_without_manager_does_not_construct_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import plugins.codex.manager as manager_module

    def forbidden_constructor(_context):
        raise AssertionError("shutdown constructed a manager")

    monkeypatch.setattr(manager_module, "CodexQueueManager", forbidden_constructor)

    await codex_main.shutdown(FakeContext(tmp_path))
    assert manager_module._MANAGER is None


@pytest.mark.asyncio
async def test_codex_manager_is_singleton_and_rejects_context_replacement(tmp_path: Path):
    import plugins.codex.manager as manager_module

    context = FakeContext(tmp_path)
    first, second = await asyncio.gather(
        manager_module.get_manager(context),
        manager_module.get_manager(context),
    )
    assert first is second

    other = FakeContext(tmp_path / "other")
    with pytest.raises(RuntimeError, match="different data directory"):
        await manager_module.get_manager(other)

    first.shutting_down = True
    with pytest.raises(RuntimeError, match="shutting down"):
        await manager_module.get_manager(context)


@pytest.mark.asyncio
async def test_cached_codex_manager_reads_live_private_override_rotation_and_deletion(
    tmp_path: Path,
):
    import plugins.codex.manager as manager_module
    from core.config import ConfigManager

    default_cwd = tmp_path / "default-cwd"
    config_path = tmp_path / "config.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "plugins": {
                    "codex": {
                        "default_cwd": str(default_cwd),
                        "allowed_cwd_roots": [str(tmp_path)],
                        "max_parallel_jobs": 1,
                        "approval_policy": "never",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    secrets_path.write_text(
        json.dumps(
            {
                "plugins": {
                    "codex": {
                        "max_parallel_jobs": 2,
                        "approval_policy": "on-request",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    source = ConfigManager(config_path, secrets_path)
    stale_snapshot = source.snapshot()

    def read_settings() -> PluginSettingsSnapshot:
        snapshot = source.snapshot()
        return PluginSettingsSnapshot(
            config=snapshot.config,
            secrets=snapshot.secrets,
            revision=snapshot.revision,
            config_status=snapshot.config_status.value,
            secrets_status=snapshot.secrets_status.value,
        )

    context = SimpleNamespace(
        data_dir=tmp_path / "plugin-data",
        config=stale_snapshot.config,
        secrets=stale_snapshot.secrets,
        get_settings_snapshot=read_settings,
        get_secret=lambda _path: (_ for _ in ()).throw(
            AssertionError("paired Codex settings must not use field reads")
        ),
    )

    manager = await manager_module.get_manager(context)
    first_runner = manager.runner
    limiter = manager.global_sem
    assert manager.config.approval_policy == "on-request"
    assert manager.config.max_parallel_jobs == 2

    source.set_plugin_secret("codex", "approval_policy", "on-failure")
    source.set_plugin_secret("codex", "max_parallel_jobs", 3)
    assert await manager_module.get_manager(context) is manager
    assert manager.runner is not first_runner
    assert manager.global_sem is limiter
    assert manager.config.approval_policy == "on-failure"
    assert manager.config.max_parallel_jobs == 3

    assert source.delete_plugin_secret("codex", "approval_policy") is True
    assert source.delete_plugin_secret("codex", "max_parallel_jobs") is True
    assert await manager_module.get_manager(context) is manager
    assert manager.config.approval_policy == "never"
    assert manager.config.max_parallel_jobs == 1
    assert stale_snapshot.secrets["plugins"]["codex"]["approval_policy"] == "on-request"


@pytest.mark.asyncio
async def test_codex_get_manager_reads_settings_inside_publication_lock(tmp_path: Path):
    import plugins.codex.manager as manager_module
    from core.config import ConfigSnapshot

    default_cwd = tmp_path / "default-cwd"
    old = ConfigSnapshot(
        config={
            "plugins": {
                "codex": {
                    "default_cwd": str(default_cwd),
                    "allowed_cwd_roots": [str(tmp_path)],
                    "max_parallel_jobs": 1,
                    "sandbox": "workspace-write",
                }
            }
        },
        secrets={},
        revision=1,
    )
    new = ConfigSnapshot(
        config={
            "plugins": {
                "codex": {
                    "default_cwd": str(default_cwd),
                    "allowed_cwd_roots": [str(tmp_path)],
                    "max_parallel_jobs": 2,
                    "sandbox": "read-only",
                }
            }
        },
        secrets={},
        revision=2,
    )
    current = {"snapshot": old}
    reads: list[int] = []

    def read_settings() -> PluginSettingsSnapshot:
        snapshot = current["snapshot"]
        reads.append(snapshot.revision)
        return PluginSettingsSnapshot(
            config=snapshot.config,
            secrets=snapshot.secrets,
            revision=snapshot.revision,
        )

    context = SimpleNamespace(
        data_dir=tmp_path / "plugin-data",
        get_settings_snapshot=read_settings,
    )
    manager = await manager_module.get_manager(context)
    singleton_lock = manager_module._manager_lock()
    await singleton_lock.acquire()
    waiting = asyncio.create_task(manager_module.get_manager(context))
    try:
        await asyncio.sleep(0)
        reads_while_locked = list(reads)
        current["snapshot"] = new
    finally:
        singleton_lock.release()

    assert await waiting is manager
    assert reads_while_locked == [1]
    assert reads == [1, 2]
    assert manager.settings_revision == 2
    assert manager.config.max_parallel_jobs == 2
    assert manager.config.sandbox == "read-only"


@pytest.mark.asyncio
async def test_codex_parallel_limit_resize_preserves_outstanding_permits(tmp_path: Path):
    from dataclasses import replace

    context = FakeContext(tmp_path, max_parallel_jobs=2)
    manager = _install_fake_manager(context, FakeRunner())
    limiter = manager.global_sem
    await limiter.acquire()
    await limiter.acquire()

    await manager.reconfigure(
        context,
        replace(manager.config, max_parallel_jobs=1),
    )
    waiter = asyncio.create_task(limiter.acquire())
    try:
        limiter.release()
        await asyncio.sleep(0)
        assert waiter.done() is False

        limiter.release()
        assert await asyncio.wait_for(waiter, timeout=1) is True
    finally:
        if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
            limiter.release()
        elif not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_codex_manager_rejects_late_stale_settings_revision(tmp_path: Path):
    from dataclasses import replace

    context = FakeContext(tmp_path, max_parallel_jobs=1)
    manager = _install_fake_manager(context, FakeRunner())
    old_config = manager.config
    new_config = replace(
        old_config,
        sandbox="read-only",
        approval_policy="on-request",
    )

    assert (
        await manager.reconfigure(
            context,
            new_config,
            settings_revision=2,
        )
        is True
    )
    assert (
        await manager.reconfigure(
            context,
            old_config,
            settings_revision=1,
        )
        is False
    )

    assert manager.settings_revision == 2
    assert manager.config is new_config
    assert manager.config.sandbox == "read-only"
    assert manager.config.approval_policy == "on-request"


@pytest.mark.asyncio
async def test_queued_codex_job_refreshes_public_policy_without_new_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import plugins.codex.manager as manager_module
    from core.app import XiaoQingApp

    calls: list[dict[str, Any]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class GenerationRunner:
        def __init__(self, config, output_dir):
            self.config = config
            self.output_dir = output_dir

        async def run(
            self,
            *,
            cwd,
            prompt,
            thread_id,
            job,
            artifact_dir=None,
            process_handoff=None,
            prompt_handoff=None,
        ):
            if process_handoff is not None and not await process_handoff(None):
                raise AssertionError("job was unexpectedly cancelled before runner start")
            if prompt_handoff is not None and not await prompt_handoff():
                raise AssertionError("job was unexpectedly cancelled before prompt")
            calls.append(
                {
                    "prompt": prompt,
                    "runner": self,
                    "sandbox": self.config.sandbox,
                    "approval_policy": self.config.approval_policy,
                    "allowed_cwd_roots": self.config.allowed_cwd_roots,
                }
            )
            if prompt == "block active":
                first_started.set()
                await release_first.wait()
            return CodexRunResult(
                exit_code=0,
                thread_id=thread_id or "thread-policy-refresh",
                final_text=f"done: {prompt}",
                stdout_tail="",
                stderr_tail="",
            )

    root = tmp_path / "app"
    config_dir = root / "config"
    plugin_dir = root / "plugins" / "codex"
    data_dir = plugin_dir / "data"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    default_cwd = root / "default-cwd"
    config_path = config_dir / "config.json"
    secrets_path = config_dir / "secrets.json"

    def write_config(*, sandbox: str, approval_policy: str, allowed_root: Path) -> None:
        config_path.write_text(
            json.dumps(
                {
                    "bot_name": "test-bot",
                    "command_prefixes": ["/"],
                    "enable_ws_client": False,
                    "enable_inbound_server": False,
                    "enable_plugin_watcher": False,
                    "timezone": "Asia/Shanghai",
                    "default_group_ids": [],
                    "plugins": {
                        "codex": {
                            "default_cwd": str(default_cwd),
                            "allowed_cwd_roots": [str(allowed_root)],
                            "max_parallel_jobs": 1,
                            "sandbox": sandbox,
                            "approval_policy": approval_policy,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    write_config(
        sandbox="workspace-write",
        approval_policy="never",
        allowed_root=tmp_path,
    )
    secrets_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("core.app.setup_logging", lambda *_args, **_kwargs: SimpleNamespace())
    app = XiaoQingApp(root)
    context = app._build_plugin_context("codex", plugin_dir, data_dir, {})
    assert context.config_manager is None
    assert callable(context.get_settings_snapshot)
    monkeypatch.setattr(manager_module, "CodexRunner", GenerationRunner)

    manager = await manager_module.get_manager(context)
    limiter = manager.global_sem
    await manager.create_session("active", None, user_id=1, group_id=None)
    await manager.create_session("waiting", None, user_id=1, group_id=None)
    await manager.enqueue(
        "active",
        "block active",
        user_id=1,
        group_id=None,
        context=context,
        metadata={"suppress_delivery": True},
    )
    await first_started.wait()

    original_settings_reader = context.settings_reader
    waiting_worker_refreshed = asyncio.Event()

    def track_waiting_worker_refresh():
        assert original_settings_reader is not None
        settings = original_settings_reader()
        waiting_worker_refreshed.set()
        return settings

    context.settings_reader = track_waiting_worker_refresh
    await manager.enqueue(
        "waiting",
        "queued after reload",
        user_id=1,
        group_id=None,
        context=context,
        metadata={"suppress_delivery": True},
    )
    await waiting_worker_refreshed.wait()

    write_config(
        sandbox="read-only",
        approval_policy="on-request",
        allowed_root=default_cwd,
    )
    app.config_manager.reload()
    # Deliberately do not call get_manager again: the queue must refresh from
    # the scoped settings reader when it advances on its own.  The raw
    # ConfigManager is intentionally unavailable to plugins.
    release_first.set()
    await manager.wait_idle()

    assert [call["prompt"] for call in calls] == ["block active", "queued after reload"]
    assert calls[0]["sandbox"] == "workspace-write"
    assert calls[0]["approval_policy"] == "never"
    assert calls[1]["sandbox"] == "read-only"
    assert calls[1]["approval_policy"] == "on-request"
    assert calls[1]["allowed_cwd_roots"] == (str(default_cwd),)
    assert calls[0]["runner"] is not calls[1]["runner"]
    assert manager.global_sem is limiter
    assert manager.config.sandbox == "read-only"
    app.scheduler.shutdown()


@pytest.mark.asyncio
async def test_codex_limit_shrink_requeues_old_limit_permit_before_start(tmp_path: Path):
    from core.config import ConfigSnapshot

    context = FakeContext(tmp_path, max_parallel_jobs=2)
    runner = FakeRunner()
    manager = _install_fake_manager(context, runner)
    old_snapshot = ConfigSnapshot(
        config=context.config,
        secrets={},
        revision=1,
    )
    old_plugin_config = dict(context.config["plugins"]["codex"])
    new_snapshot = ConfigSnapshot(
        config={
            "plugins": {
                "codex": {
                    **old_plugin_config,
                    "max_parallel_jobs": 1,
                    "sandbox": "read-only",
                }
            }
        },
        secrets={},
        revision=2,
    )
    reads = 0
    shrink_published = asyncio.Event()

    def read_settings() -> PluginSettingsSnapshot:
        nonlocal reads
        reads += 1
        snapshot = new_snapshot if reads >= 4 else old_snapshot
        if reads == 4:
            shrink_published.set()
        return PluginSettingsSnapshot(
            config=snapshot.config,
            secrets=snapshot.secrets,
            revision=snapshot.revision,
        )

    context.get_settings_snapshot = read_settings
    await manager.create_session("active", None, user_id=1, group_id=None)
    await manager.create_session("waiting", None, user_id=1, group_id=None)

    await manager.enqueue(
        "active",
        "block",
        user_id=1,
        group_id=None,
        context=context,
        metadata={"suppress_delivery": True},
    )
    await _wait_until(lambda: runner.started == ["active"])
    await manager.enqueue(
        "waiting",
        "after shrink",
        user_id=1,
        group_id=None,
        context=context,
        metadata={"suppress_delivery": True},
    )
    await shrink_published.wait()
    await asyncio.sleep(0)

    assert manager.config.max_parallel_jobs == 1
    assert manager.config.sandbox == "read-only"
    assert runner.started == ["active"]

    runner.release.set()
    await manager.wait_idle()

    assert runner.started == ["active", "waiting"]
    assert reads >= 5


@pytest.mark.asyncio
async def test_arxiv_summary_replays_existing_success_without_rerun(tmp_path: Path):
    context = FakeContext(tmp_path)
    runner = FakeRunner(
        result_text=_valid_arxiv_summary(
            "2026-05-19",
            "https://arxiv.org/abs/2605.16917",
            "cached summary",
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

    result = await _arxiv_addon(manager).enqueue_or_replay(
        date="2026-05-19",
        links=["https://arxiv.org/abs/2605.16917"],
        user_id=1,
        group_id=2,
        context=context,
    )

    assert "已重发" in result
    assert len(runner.calls) == 2
    sent_text = str(context.actions)
    assert "[codex:astro-ph #2] 完成" in sent_text
    assert "cached summary" in sent_text
