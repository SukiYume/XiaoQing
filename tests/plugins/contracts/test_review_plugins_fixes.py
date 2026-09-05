"""跨插件完整审查的隔离回归：取消、数据恢复、协议边界与数值契约。"""

import asyncio
import importlib
import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_review_smalltalk_cancel_preserves_committed_snapshot(tmp_path, monkeypatch):
    from plugins.smalltalk import main

    context = SimpleNamespace(data_dir=tmp_path, current_user_id=1, current_group_id=None)
    started, release = threading.Event(), threading.Event()
    original = main._write_qa_file

    def delayed(path, data):
        started.set()
        assert release.wait(3)
        original(path, data)

    monkeypatch.setattr(main, "_write_qa_file", delayed)
    task = asyncio.create_task(main._add_qa(context, "first answer"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(main, "_write_qa_file", original)
        await main._add_qa(context, "second answer")
        assert json.loads(main._qa_file(context).read_text()) == {
            "first": ["answer"],
            "second": ["answer"],
        }
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        main._qa_snapshot.cache_clear()
        main._audit_snapshot.cache_clear()


@pytest.mark.asyncio
async def test_review_chat_cancel_compensates_quota(tmp_path, monkeypatch):
    from plugins.chat import main

    context = SimpleNamespace(data_dir=tmp_path, now=lambda: datetime.now(UTC))
    started, release = threading.Event(), threading.Event()
    original = main._reserve_quota_file

    def delayed(*args):
        started.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(main, "_reserve_quota_file", delayed)
    task = asyncio.create_task(
        main._reserve_quota(context, actor="123", per_user_limit=20, global_limit=100)
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = json.loads(main._quota_path(context).read_text())
        assert state["total"] == 0 and state["users"] == {}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_review_adnmb_malformed_html_is_bounded():
    from plugins.adnmb.adapi import Post

    start = time.monotonic()
    post  = Post.from_json({"content": "<" * 500_000})
    assert len(post.content) == 65_536
    assert time.monotonic() - start < 1
    assert Post.from_json({"content": "a<b>c</b>"}).content == "ac"


def test_review_shell_timeout_reaps_inherited_pipe_child(tmp_path):
    from plugins.shell.main import _execute_command

    marker = tmp_path / "escaped.txt"
    child  = (
        "import time; from pathlib import Path; time.sleep(1); Path("
        + repr(str(marker))
        + ").write_text('escaped')"
    )
    parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"

    async def exercise():
        start = time.monotonic()
        code, _stdout, _stderr = await _execute_command([sys.executable, "-c", parent], 0.2)
        assert code == -1
        assert time.monotonic() - start < 0.9
        await asyncio.sleep(1.05)
        assert not marker.exists()

    factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(exercise())


@pytest.mark.asyncio
async def test_review_qingssh_cancel_owns_opening_channel(tmp_path, monkeypatch):
    from plugins.qingssh.ssh_manager import SSHManager

    started, release = threading.Event(), threading.Event()
    channel = SimpleNamespace(closed=False, executed=False)
    channel.close              = lambda: setattr(channel, "closed", True)
    channel.set_combine_stderr = lambda value: None
    channel.exec_command       = lambda command: setattr(channel, "executed", True)

    def open_session():
        started.set()
        assert release.wait(3)
        return channel

    transport = SimpleNamespace(is_active=lambda: True, open_session=open_session)
    manager = SSHManager(tmp_path)
    manager.connections["1:None:example"] = SimpleNamespace(get_transport=lambda: transport)
    task = asyncio.create_task(
        manager.execute_command_stream("1", None, "example", "review-only", AsyncMock())
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert channel.closed and not channel.executed
        assert not manager.active_channels
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_review_qingssh_failed_image_delivery_is_reported(tmp_path):
    from plugins.qingssh.session_handlers import _handle_showimg_command

    async def download(*args, **kwargs):
        Path(args[4]).write_bytes(b"test")
        return True, "ok"

    manager = SimpleNamespace(
        is_connected=lambda *args: True,
        list_files=AsyncMock(return_value=(True, ["one.png"])),
        download_file=download,
    )
    context = SimpleNamespace(
        data_dir         = tmp_path,
        current_user_id  = 1,
        current_group_id = None,
        send_action=AsyncMock(return_value=False),
    )
    result = await _handle_showimg_command(
        "showimg *.png", context, {"server_name": "test", "cwd": "/tmp"}, manager
    )
    output = "".join(part.get("data", {}).get("text", "") for part in result)
    assert "发送 0 张" in output and "投递失败" in output


@pytest.mark.parametrize("blue", [100, 128, 255])
def test_review_color_rgb_three_digit_blue(blue):
    from plugins.color.main import _parse_custom_color

    result = _parse_custom_color(f"品牌 蓝 51 102 {blue}")
    assert result["name"] == "品牌 蓝"
    assert result["RGB"] == [51, 102, blue]


@pytest.mark.asyncio
async def test_review_rcon_partial_continuation_discards_connection():
    from plugins.minecraft.rcon import PacketType, RconClient, RconPacket

    reader = asyncio.StreamReader()
    writer = SimpleNamespace(
        is_closing  = lambda: False,
        write       = lambda data: None,
        drain       = AsyncMock(),
        close       = lambda: None,
        wait_closed = AsyncMock(),
    )
    client = RconClient("localhost", 25575, "fake", timeout=0.1)
    client.RESPONSE_CHUNK_TIMEOUT = 0.01
    client._reader, client._writer, client._connected = reader, writer, True
    reader.feed_data(RconPacket(1, PacketType.RESPONSE, "a" * 4096).encode())
    reader.feed_data(RconPacket(1, PacketType.RESPONSE, "tail").encode()[:8])
    result = await client.command("list")
    assert not result.success and not client.connected


@pytest.mark.asyncio
async def test_review_flickr_empty_queries_do_not_accumulate_locks(tmp_path):
    from plugins.flickr import main

    context = SimpleNamespace(state={}, current_user_id=1, current_group_id=None)
    for user in range(1, 1100):
        context.current_user_id = user
        await main._more(1, event={}, context=context)
    runtime = main._runtime(context)
    main._prune_runtime(runtime, now=float("inf"))
    assert runtime["sessions"] == runtime["locks"] == {}


@pytest.mark.asyncio
async def test_review_twitter_error_never_completes_backfill(tmp_path, monkeypatch):
    from core.bounded_http import BoundedHttpResponse
    from plugins.twitter import main

    body     = json.dumps({"errors": [{"message": "temporary"}]}).encode()
    response = BoundedHttpResponse(
        "https://x.com/", 200, body, "application/json", "utf-8", {}, len(body), len(body)
    )
    settings = SimpleNamespace(plugin_secrets=lambda name: {"user_id": "123"})
    context = SimpleNamespace(
        data_dir=tmp_path, http_session=object(), get_settings_snapshot=lambda: settings
    )
    monkeypatch.setattr(main, "aiohttp_request_bounded", AsyncMock(return_value=response))
    with pytest.raises(main.TwitterFetchError):
        await main._fetch_twitter_images(context)
    assert not main._backfill_is_complete(tmp_path / main.BACKFILL_STATE_FILENAME, "123")


@pytest.mark.asyncio
async def test_review_jupyter_status_keeps_loop_responsive(tmp_path, monkeypatch):
    from plugins.jupyter import main
    from plugins.jupyter.jupyter_manager import JupyterKernelManager

    manager = JupyterKernelManager(tmp_path)
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with manager._lifecycle_lock:
            acquired.set()
            release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert await asyncio.to_thread(acquired.wait, 3)
    monkeypatch.setattr(JupyterKernelManager, "get_instance", lambda *args: manager)
    context = SimpleNamespace(data_dir=tmp_path, current_user_id=1, current_group_id=None)
    task = asyncio.create_task(main._handle_kernel("status", context))
    try:
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
        await task
        thread.join()


@pytest.mark.parametrize(
    "source,target,expected",
    [
        ("MJy", "Jy", "1e+06"),
        ("MK", "K", "1e+06"),
        ("mpc", "pc", "0.001"),
        ("Jy", "MJy", "1.000000e-06"),
    ],
)
def test_review_astro_units_preserve_case(source, target, expected):
    from plugins.astro_tools.convert import _handle_convert_sync

    output = _handle_convert_sync(
        f"1 {source} {target}", SimpleNamespace(logger=logging.getLogger(__name__))
    )
    assert f"= {expected} {target}" in output


def test_review_luminosity_uses_complete_empirical_coefficients():
    from plugins.astro_tools.formula import _handle_calculation

    context = SimpleNamespace(logger=logging.getLogger(__name__))
    assert "5.000e+04" in _handle_calculation("luminosity 20", context)
    assert "5.357e+04" in _handle_calculation("luminosity 19.99", context)


@pytest.mark.asyncio
async def test_review_ads_author_requests_latest_sort(monkeypatch):
    from plugins.ads_paper.ads_client import ADSClient

    search = AsyncMock(return_value=[])
    monkeypatch.setattr(ADSClient, "search_papers", search)
    client = ADSClient.__new__(ADSClient)
    await client.search_by_author("review author")
    assert search.await_args.kwargs["sort"] == "date desc"


@pytest.mark.asyncio
async def test_review_ads_reference_preview_is_labeled(monkeypatch):
    from plugins.ads_paper import paper_commands

    monkeypatch.setattr(
        paper_commands, "resolve_paper_id_to_bibcode", AsyncMock(return_value="2020Review")
    )
    client = SimpleNamespace(
        get_paper_by_bibcode=AsyncMock(return_value={"title": ["review"], "citation_count": 42}),
        get_citations=AsyncMock(return_value=[]),
        get_references=AsyncMock(return_value=[{"title": ["reference"]}] * 5),
    )
    response = await paper_commands.cmd_cite_network(client, "review")
    text     = "".join(segment["data"].get("text", "") for segment in response)
    assert "本次展示参考文献: 5 篇" in text


@pytest.mark.parametrize("broken", [[], {"schema_version": 999, "sessions": {}}])
def test_review_codex_schema_failure_recovers_valid_backup(tmp_path, broken):
    from plugins.codex.manager import CodexQueueManager, CodexSession
    from tests.helpers.codex_test_support import FakeContext, FakeRunner

    context = FakeContext(tmp_path)
    context.default_cwd.mkdir()
    manager = CodexQueueManager(context, runner=FakeRunner())
    manager.sessions["kept"] = CodexSession(
        label           = "kept",
        cwd             = str(context.default_cwd),
        owner_user_id   = 1,
        target_group_id = None,
        thread_id       = "review-thread",
    )
    manager._rewrite_state_with_backup()
    manager.sessions_path.write_text(json.dumps(broken))
    recovered = CodexQueueManager(context, runner=FakeRunner())
    assert recovered.sessions["kept"].thread_id == "review-thread"
    backup = json.loads(recovered.sessions_path.with_suffix(".json.bak").read_text())
    assert backup["sessions"]["kept"]["thread_id"] == "review-thread"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "date"])
async def test_review_arxiv_cancellation_releases_claim(tmp_path, monkeypatch, stage):
    from plugins.arxiv_filter import main

    entered, release = threading.Event(), threading.Event()
    context = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(main, "_scheduled_without_delivery_targets", lambda context: False)
    monkeypatch.setattr(main, "_business_now", lambda context: datetime(2030, 1, 1, tzinfo=UTC))
    original = main._claim_send_today

    def delayed_claim(*args):
        result = original(*args)
        entered.set()
        assert release.wait(3)
        return result

    async def date_call(func, *args):
        if func is original or func is main._release_claim:
            return await asyncio.to_thread(func, *args)
        entered.set()
        await asyncio.Event().wait()

    if stage == "claim":
        monkeypatch.setattr(main, "_claim_send_today", delayed_claim)
    else:
        monkeypatch.setattr(main, "run_sync", date_call)
    task = asyncio.create_task(main._check_arxiv_update(context))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not main._claim_path(tmp_path, "2030-01-01").exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_review_arxiv_cache_requires_matching_range(tmp_path, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, "feedparser", SimpleNamespace())
        module = importlib.import_module(
            "plugins.arxiv_filter.train_model.data_prep.step2_fetch_all_astro_ph"
        )
    monkeypatch.setattr(module, "MONTHLY_DIR", tmp_path)
    module.save_cache(2401, [], api_start="20240101", api_end="20240110")
    assert module.is_month_finalized(2401, api_start="20240101", api_end="20240110")
    assert not module.is_month_finalized(2401, api_start="20240101", api_end="20240131")
    result = module.FetchResult([], False, 100, 200)
    module.save_checkpoint(2401, result, api_start="20240101", api_end="20240110")
    assert module.load_checkpoint(2401, api_start="20240101", api_end="20240131").next_offset == 0


@pytest.mark.parametrize("stale", [False, True])
def test_review_arxiv_dataset_enforces_current_range(tmp_path, monkeypatch, stale):
    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, "feedparser", SimpleNamespace())
        module = importlib.import_module(
            "plugins.arxiv_filter.train_model.data_prep.step3_build_dataset"
        )
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(module, "POSITIVE_IDS_CSV", tmp_path / "positive_ids.csv")
    monkeypatch.setattr(module, "OUTPUT_CSV", tmp_path / "output.csv")
    (tmp_path / "positive_ids.csv").write_text("arXiv ID\n2401.00001\n")
    (tmp_path / "date_range.json").write_text(
        json.dumps({"start": "2024-01-01", "end": "2024-01-31"})
    )
    monthly = tmp_path / "monthly"
    monthly.mkdir()
    (monthly / "2401.json").write_text(
        json.dumps(
            {
                "completed": True,
                "query_range": ["202401010000", "202401102359" if stale else "202401312359"],
                "papers": [
                    {
                        "arxiv_id": "2401.00001",
                        "title": "Review sample",
                        "abstract": "An isolated astronomy abstract.",
                    }
                ],
            }
        )
    )
    if stale:
        with pytest.raises(ValueError, match="step 2"):
            module.main()
        assert not module.OUTPUT_CSV.exists()
    else:
        module.main()
        assert "2401.00001" in module.OUTPUT_CSV.read_text()


def test_review_qingpet_backup_contains_committed_wal(tmp_path):
    from plugins.qingpet.services.database import Database

    path   = tmp_path / "pet.db"
    source = sqlite3.connect(path)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE review_marker(value INTEGER)")
    source.commit()
    source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    source.execute("INSERT INTO review_marker VALUES (42)")
    source.commit()
    database = Database(str(path))
    try:
        backup = sqlite3.connect(str(path) + ".pre-migration.bak")
        try:
            assert backup.execute("SELECT value FROM review_marker").fetchall() == [(42,)]
        finally:
            backup.close()
    finally:
        database.cleanup()
        source.close()


def test_review_qingpet_day_boundary_is_shanghai(monkeypatch):
    from plugins.qingpet.services import database_clock
    from plugins.qingpet.utils import time as clock

    for value, expected in [
        (datetime(2030, 1, 1, 15, 59), "2030-01-01"),
        (datetime(2030, 1, 1, 16), "2030-01-02"),
        (datetime(2030, 1, 2), "2030-01-02"),
    ]:
        monkeypatch.setattr(database_clock, "now", lambda value=value: value)
        monkeypatch.setattr(clock, "utc_now", lambda value=value: value)
        assert database_clock.business_date() == clock.business_date() == expected


def test_review_qingpet_decay_persists_fractional_progress(tmp_path, monkeypatch):
    from plugins.qingpet.services import pet_service
    from plugins.qingpet.services.database import Database

    database = Database(str(tmp_path / "decay.db"))
    base     = datetime(2030, 1, 1)
    current  = base
    monkeypatch.setattr(pet_service, "utc_now", lambda: current)
    service = pet_service.PetService(database)
    try:
        for actor in ("a", "b"):
            assert service.adopt_pet(actor, 1, actor)[0]
            pet             = database.get_pet(actor, 1)
            pet.last_update = base
            assert database.update_pet(pet)
        for minute in range(1, 21):
            current = base + timedelta(minutes=minute)
            service.apply_decay(database.get_pet("a", 1), is_trustee_override=False)
        service.apply_decay(database.get_pet("b", 1), is_trustee_override=False)
        a, b = database.get_pet("a", 1), database.get_pet("b", 1)
        assert [getattr(a, key) for key in ("hunger", "mood", "clean", "energy", "health")] == [
            getattr(b, key) for key in ("hunger", "mood", "clean", "energy", "health")
        ]
    finally:
        database.cleanup()
