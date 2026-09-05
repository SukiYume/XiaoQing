from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.interfaces import PluginCapabilities, PluginPrincipal
from core.plugin_execution import PluginExecutionClosed
from tests.helpers.settings_snapshot import with_settings_reader

arxiv_filter = importlib.import_module("plugins.arxiv_filter.main")


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


def _status_aware_run_sync(date_result: str):
    async def run_sync_for_test(function, *args, **kwargs):
        if function in (
            arxiv_filter._claim_send_today,
            arxiv_filter._release_claim,
            arxiv_filter._mark_sent_today,
        ):
            return function(*args, **kwargs)
        return date_result

    return run_sync_for_test


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_result",
    [
        None,
        [{"type": "text", "data": {"text": "论文筛选服务暂时不可用"}}],
        [{"type": "text", "data": {"text": "任意公开错误文本"}}],
    ],
)
async def test_unknown_or_unstructured_filter_results_never_mark_sent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filter_result,
) -> None:
    context = _context(tmp_path)
    today   = arxiv_filter._business_now(context).date().isoformat()
    monkeypatch.setattr(arxiv_filter, "run_sync", _status_aware_run_sync(today))
    monkeypatch.setattr(arxiv_filter, "_run_filter", AsyncMock(return_value=filter_result))

    await arxiv_filter._check_arxiv_update(context)

    assert arxiv_filter._should_send_today(context.data_dir, today) is True
    assert not Path(arxiv_filter._claim_path(context.data_dir, today)).exists()


@pytest.mark.asyncio
async def test_filter_exception_releases_claim_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path)
    today   = arxiv_filter._business_now(context).date().isoformat()
    monkeypatch.setattr(arxiv_filter, "run_sync", _status_aware_run_sync(today))
    monkeypatch.setattr(
        arxiv_filter,
        "_run_filter",
        AsyncMock(side_effect=RuntimeError("backend failed")),
    )

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
    today   = arxiv_filter._business_now(context).date().isoformat()
    monkeypatch.setattr(arxiv_filter, "run_sync", _status_aware_run_sync(today))
    successful = arxiv_filter._filter_result(
        "papers",
        succeeded = True,
        outcome   = "papers",
    )
    monkeypatch.setattr(arxiv_filter, "_run_filter", AsyncMock(return_value=successful))

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
    today   = arxiv_filter._business_now(context).date().isoformat()
    monkeypatch.setattr(arxiv_filter, "run_sync", _status_aware_run_sync(today))
    successful = arxiv_filter._filter_result("papers", succeeded=True, outcome="papers")
    monkeypatch.setattr(arxiv_filter, "_run_filter", AsyncMock(return_value=successful))

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
    today   = arxiv_filter._business_now(context).date().isoformat()
    monkeypatch.setattr(arxiv_filter, "run_sync", _status_aware_run_sync("2000-01-01"))

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
    today   = arxiv_filter._business_now(context).date().isoformat()
    assert arxiv_filter._claim_send_today(context.data_dir, today) is True
    result = arxiv_filter._filter_result("papers", succeeded=True, outcome="papers")
    tracked = arxiv_filter._track_delivery(result, context.data_dir, today)

    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(arxiv_filter, "atomic_write_text", fail_write)

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
    context   = _context(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "training_config.json").write_text("{}", encoding="utf-8")
    configs = iter(
        [
            {"model": {"path": str(model_dir), "threshold": 0.5}},
            {"model": {"path": str(model_dir), "threshold": 0.7}},
        ]
    )
    calls = 0

    def inference(**_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "No positive predictions found."

    arxiv_filter._FILTER_CACHE.clear()
    monkeypatch.setattr(arxiv_filter, "load_plugin_config", lambda: next(configs))
    monkeypatch.setattr(arxiv_filter, "_load_inference", lambda **_kwargs: inference)

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    await arxiv_filter._run_filter(context, source_date="2026-08-05")

    assert calls == 2


@pytest.mark.asyncio
async def test_filter_cache_changes_when_arxiv_source_listing_date_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新前的昨日列表不能污染同一业务日稍后发布的新列表。"""

    context   = _context(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "training_config.json").write_text("{}", encoding="utf-8")
    calls = 0

    def inference(**_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "No positive predictions found."

    arxiv_filter._FILTER_CACHE.clear()
    monkeypatch.setattr(
        arxiv_filter,
        "load_plugin_config",
        lambda: {"model": {"path": str(model_dir)}},
    )
    monkeypatch.setattr(arxiv_filter, "_load_inference", lambda **_kwargs: inference)

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    await arxiv_filter._run_filter(context, source_date="2026-08-06")

    assert calls == 2
    assert {key[0] for key in arxiv_filter._FILTER_CACHE} == {"2026-08-06"}


@pytest.mark.asyncio
async def test_daily_cache_reuses_one_model_tree_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一模型的缓存命令不能再次递归扫描、stat 或 hash 整棵目录。"""

    context   = _context(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "training_config.json").write_text("{}", encoding="utf-8")
    shared = importlib.import_module("plugins.arxiv_filter.inference.shared")
    original_fingerprint = shared.model_artifact_fingerprint
    fingerprint_calls = 0
    inference_kwargs: list[dict[str, object]] = []

    def counted_fingerprint(model_path: str) -> str:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return original_fingerprint(model_path)

    def inference(**kwargs) -> str:
        inference_kwargs.append(kwargs)
        return "No positive predictions found."

    arxiv_filter._FILTER_CACHE.clear()
    cached_fingerprints = getattr(arxiv_filter, "_MODEL_FINGERPRINT_CACHE", None)
    if isinstance(cached_fingerprints, dict):
        cached_fingerprints.clear()
    monkeypatch.setattr(
        arxiv_filter,
        "load_plugin_config",
        lambda: {"model": {"path": str(model_dir)}},
    )
    monkeypatch.setattr(arxiv_filter, "_load_inference", lambda **_kwargs: inference)
    monkeypatch.setattr(shared, "model_artifact_fingerprint", counted_fingerprint)

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    await arxiv_filter._run_filter(context, source_date="2026-08-05")

    assert fingerprint_calls == 1
    assert len(inference_kwargs) == 1
    assert isinstance(inference_kwargs[0].get("artifact_fingerprint"), str)


@pytest.mark.asyncio
async def test_model_fingerprint_refresh_detects_live_artifact_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """刷新窗口到期后，模型替换必须生成新缓存键并重新推理。"""

    context   = _context(tmp_path)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    artifact = model_dir / "meta.json"
    artifact.write_text('{"version": 1}', encoding="utf-8")
    current_monotonic = {"value": 0.0}
    inference_calls   = 0

    def inference(**_kwargs) -> str:
        nonlocal inference_calls
        inference_calls += 1
        return "No positive predictions found."

    arxiv_filter._FILTER_CACHE.clear()
    arxiv_filter._MODEL_FINGERPRINT_CACHE.clear()
    monkeypatch.setattr(
        arxiv_filter,
        "load_plugin_config",
        lambda: {"model": {"path": str(model_dir)}},
    )
    monkeypatch.setattr(arxiv_filter, "_load_inference", lambda **_kwargs: inference)
    monkeypatch.setattr(
        arxiv_filter.time,
        "monotonic",
        lambda: current_monotonic["value"],
    )

    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    current_monotonic["value"] = 10.0
    await arxiv_filter._run_filter(context, source_date="2026-08-05")
    artifact.write_text('{"version": 2}', encoding="utf-8")
    current_monotonic["value"] = 61.0
    await arxiv_filter._run_filter(context, source_date="2026-08-05")

    assert inference_calls == 2


def test_precomputed_artifact_fingerprint_avoids_backend_rescan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入口传入的稳定指纹应直接成为后端缓存身份。"""

    shared = importlib.import_module("plugins.arxiv_filter.inference.shared")
    params = shared.InferenceParams(
        model_path           = "model",
        threshold            = 0.5,
        batch_size           = 2,
        max_len              = 32,
        artifact_fingerprint = "stable-artifact-identity",
    )
    monkeypatch.setattr(
        shared,
        "model_artifact_fingerprint",
        lambda _path: pytest.fail("precomputed identity must not rescan the model tree"),
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
