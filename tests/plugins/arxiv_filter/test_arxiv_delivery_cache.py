from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.interfaces import PluginCapabilities, PluginPrincipal
from core.plugin_execution import PluginExecutionClosed
from tests.helpers.arxiv_filter_test_support import ArxivWorld, use_plugin_config, write_knn_model
from tests.helpers.settings_snapshot import with_settings_reader

arxiv_filter       = importlib.import_module("plugins.arxiv_filter.main")
arxiv_filter_utils = importlib.import_module("plugins.arxiv_filter.utils")


def _context(tmp_path: Path) -> SimpleNamespace:
    plugin_dir = tmp_path / "plugin-code"
    data_dir   = tmp_path / "persistent-data"
    plugin_dir.mkdir(exist_ok=True)
    data_dir.mkdir(exist_ok=True)
    return with_settings_reader(
        SimpleNamespace(
            plugin_dir = plugin_dir,
            data_dir   = data_dir,
            config     = {},
            logger     = MagicMock(),
            principal=PluginPrincipal(kind="lifecycle"),
            capabilities=PluginCapabilities(),
        )
    )


def _today(context: SimpleNamespace) -> str:
    return arxiv_filter._business_now(context).date().isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["no-paper-list", "missing-dependency", "missing-model-files"])
async def test_failed_filter_results_never_mark_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    context = _context(tmp_path)
    today   = _today(context)
    world = ArxivWorld(monkeypatch, tmp_path, list_date=today, titles=["Pulsar timing"])
    if scenario == "no-paper-list":
        world.titles = ()
    elif scenario == "missing-dependency":
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    else:
        (world.model_dir / "meta.json").unlink()

    result = await arxiv_filter._check_arxiv_update(context)

    assert result.succeeded is False
    assert arxiv_filter._should_send_today(context.data_dir, today) is True
    assert not Path(arxiv_filter._claim_path(context.data_dir, today)).exists()


@pytest.mark.asyncio
async def test_filter_exception_releases_claim_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    today   = _today(context)
    ArxivWorld(monkeypatch, tmp_path, list_date=today, titles=["Pulsar timing"])
    # 模型配置不是 JSON 对象：筛选在推理之前就抛出异常。
    use_plugin_config(monkeypatch, tmp_path, {"model": "not-an-object"})

    result = await arxiv_filter._check_arxiv_update(context)

    assert result.succeeded is False
    assert arxiv_filter._should_send_today(context.data_dir, today) is True
    assert not Path(arxiv_filter._claim_path(context.data_dir, today)).exists()


@pytest.mark.asyncio
async def test_status_commits_only_after_true_delivery_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    today   = _today(context)
    ArxivWorld(monkeypatch, tmp_path, list_date=today, titles=["Pulsar timing"])

    first = await arxiv_filter._check_arxiv_update(context)
    assert arxiv_filter._should_send_today(context.data_dir, today) is True
    await first.delivery_receipt.record(False)
    assert arxiv_filter._should_send_today(context.data_dir, today) is True

    second = await arxiv_filter._check_arxiv_update(context)
    await second.delivery_receipt.record(True)

    assert second.delivery_receipt.committed is True
    assert arxiv_filter._should_send_today(context.data_dir, today) is False
    assert (context.data_dir / "update_status.json").is_file()
    assert not (context.plugin_dir / "data" / "update_status.json").exists()


@pytest.mark.asyncio
async def test_unknown_delivery_uses_at_most_once_daily_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    today   = _today(context)
    ArxivWorld(monkeypatch, tmp_path, list_date=today, titles=["Pulsar timing"])

    result = await arxiv_filter._check_arxiv_update(context)
    await result.delivery_receipt.record(None)

    assert result.delivery_receipt.resolved is True
    assert result.delivery_receipt.outcome is None
    assert result.delivery_receipt.committed is False
    assert arxiv_filter._should_send_today(context.data_dir, today) is False


@pytest.mark.asyncio
async def test_final_no_update_notice_also_waits_for_delivery_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    today   = _today(context)
    ArxivWorld(monkeypatch, tmp_path, list_date="2000-01-01")

    result = await arxiv_filter._check_arxiv_update(context, is_final_check=True)

    assert arxiv_filter._should_send_today(context.data_dir, today) is True
    await result.delivery_receipt.record(False)
    assert arxiv_filter._should_send_today(context.data_dir, today) is True


@pytest.mark.asyncio
async def test_status_write_failure_does_not_commit_or_hold_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    today   = _today(context)
    ArxivWorld(monkeypatch, tmp_path, list_date="2000-01-01")
    tracked = await arxiv_filter._check_arxiv_update(context, is_final_check=True)
    assert Path(arxiv_filter._claim_path(context.data_dir, today)).exists()

    atomic_store = importlib.import_module("core.atomic_store")

    def fail_replace(*_args) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic_store.os, "replace", fail_replace)

    await tracked.delivery_receipt.record(True)

    assert tracked.delivery_receipt.committed is False
    assert isinstance(tracked.delivery_receipt.callback_error, OSError)
    assert arxiv_filter._should_send_today(context.data_dir, today) is True
    assert not Path(arxiv_filter._claim_path(context.data_dir, today)).exists()


@pytest.mark.asyncio
async def test_delivery_ack_commits_after_plugin_execution_scope_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发送回执晚于插件 handler 返回时仍须可靠提交当天状态。"""

    context = _context(tmp_path)
    today   = arxiv_filter._business_now(context).date().isoformat()
    assert arxiv_filter._claim_send_today(context.data_dir, today) is True
    result = arxiv_filter._filter_result("papers", succeeded=True, outcome="papers")
    tracked = arxiv_filter._track_delivery(result, context.data_dir, today)

    async def closed_run_sync(*_args, **_kwargs):
        raise PluginExecutionClosed("plugin execution scope is no longer active")

    # 生产回执到达时，插件 bulkhead 已关闭；回执提交不能再依赖 run_sync。
    monkeypatch.setattr(arxiv_filter, "run_sync", closed_run_sync)
    await tracked.delivery_receipt.record(True)

    assert tracked.delivery_receipt.committed is True
    assert tracked.delivery_receipt.callback_error is None
    assert arxiv_filter._should_send_today(context.data_dir, today) is False
    assert not Path(arxiv_filter._claim_path(context.data_dir, today)).exists()


@pytest.mark.asyncio
async def test_daily_filter_cache_changes_with_plugin_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-05")
    config_path = use_plugin_config(
        monkeypatch,
        tmp_path,
        {"model": {"path": str(world.model_dir), "threshold": 0.5}},
    )

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    config_path.write_text(
        json.dumps({"model": {"path": str(world.model_dir), "threshold": 0.7}}),
        encoding="utf-8",
    )
    await arxiv_filter._run_filter(context, source_date="2026-08-05")

    assert len(world.requests) == 2


@pytest.mark.asyncio
async def test_filter_cache_changes_when_arxiv_source_listing_date_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新前的昨日列表不能污染同一业务日稍后发布的新列表。"""

    context = _context(tmp_path)
    world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-05")

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    await arxiv_filter._run_filter(context, source_date="2026-08-06")

    assert len(world.requests) == 2
    assert {key[0] for key in arxiv_filter._FILTER_CACHE} == {"2026-08-06"}


@pytest.mark.asyncio
async def test_daily_cache_reuses_one_model_tree_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一模型的缓存命令不能再次递归扫描整棵目录：刷新窗口内改动模型文件也不会换缓存键。"""

    context = _context(tmp_path)
    world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-05", titles=["Pulsar timing"])

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    write_knn_model(world.model_dir, version=2)
    await arxiv_filter._run_filter(context, source_date="2026-08-05")

    assert len(world.requests) == 1
    assert len(world.encoder.loads) == 1


@pytest.mark.asyncio
async def test_model_fingerprint_refresh_detects_live_artifact_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """刷新窗口到期后，模型替换必须生成新缓存键并重新推理。"""

    context = _context(tmp_path)
    world = ArxivWorld(monkeypatch, tmp_path, list_date="2026-08-05")
    current_monotonic = {"value": 0.0}
    monkeypatch.setattr(
        arxiv_filter.time,
        "monotonic",
        lambda: current_monotonic["value"],
    )

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    current_monotonic["value"] = 10.0
    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    write_knn_model(world.model_dir, version=2)
    current_monotonic["value"] = 61.0
    await arxiv_filter._run_filter(context, source_date="2026-08-05")

    assert len(world.requests) == 2


def test_precomputed_artifact_fingerprint_avoids_backend_rescan() -> None:
    """入口传入的稳定指纹应直接成为后端缓存身份；模型目录不存在，重新扫描只会得到另一个值。"""

    shared = importlib.import_module("plugins.arxiv_filter.inference.shared")
    params = shared.InferenceParams(
        model_path           = "model",
        threshold            = 0.5,
        batch_size           = 2,
        max_len              = 32,
        artifact_fingerprint = "stable-artifact-identity",
    )

    assert (
        shared.resolve_artifact_fingerprint(params, "runtime-model") == "stable-artifact-identity"
    )


def test_model_fingerprint_changes_for_same_path_content_replacement(tmp_path: Path) -> None:
    shared   = importlib.import_module("plugins.arxiv_filter.inference.shared")
    artifact = tmp_path / "meta.json"
    artifact.write_text('{"version": 1}', encoding="utf-8")
    first = shared.model_artifact_fingerprint(str(tmp_path))

    artifact.write_text('{"version": 2}', encoding="utf-8")
    second = shared.model_artifact_fingerprint(str(tmp_path))

    assert first != second


def test_model_fingerprint_ignores_training_only_outputs(tmp_path: Path) -> None:
    shared = importlib.import_module("plugins.arxiv_filter.inference.shared")
    (tmp_path / "meta.json").write_text('{"version": 1}', encoding="utf-8")
    validation = tmp_path / "validation_scored.csv"
    validation.write_text("score\n0.1\n", encoding="utf-8")
    cache_dir = tmp_path / "emb_cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "cache.npy"
    cache_file.write_bytes(b"first")
    first = shared.model_artifact_fingerprint(str(tmp_path))

    validation.write_text("score\n0.9\n", encoding="utf-8")
    cache_file.write_bytes(b"second")

    assert shared.model_artifact_fingerprint(str(tmp_path)) == first


def test_interrupted_abstract_cache_write_preserves_previous_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "feedparser", SimpleNamespace())
    module = importlib.import_module(
        "plugins.arxiv_filter.train_model.data_prep.step3_build_dataset"
    )
    cache_path  = tmp_path / "abstract_cache.json"
    old_payload = {"old": {"title": "old", "abstract": "kept"}}
    cache_path.write_text(json.dumps(old_payload), encoding="utf-8")
    monkeypatch.setattr(module, "ABSTRACT_CACHE_FILE", cache_path)
    atomic_store = importlib.import_module("core.atomic_store")

    def fail_replace(*_args) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(atomic_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="interrupted"):
        module.save_abstract_cache({"new": {"title": "new", "abstract": "pending"}})

    assert json.loads(cache_path.read_text(encoding="utf-8")) == old_payload
    assert list(tmp_path.glob(".abstract_cache.json.*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "date"])
async def test_arxiv_cancellation_releases_claim(tmp_path, monkeypatch, stage):
    from plugins.arxiv_filter import main

    entered, release = (threading.Event(), threading.Event())
    context = SimpleNamespace(data_dir=tmp_path)
    today    = main._business_now(context).date().isoformat()
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
        assert not main._claim_path(tmp_path, today).exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_arxiv_cache_requires_matching_range(tmp_path, monkeypatch):
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
def test_arxiv_dataset_enforces_current_range(tmp_path, monkeypatch, stale):
    monkeypatch.setitem(sys.modules, "feedparser", SimpleNamespace())
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
                "completed"  : True,
                "query_range": ["202401010000", "202401102359" if stale else "202401312359"],
                "papers"     : [
                    {
                        "arxiv_id": "2401.00001",
                        "title"   : "Review sample",
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
