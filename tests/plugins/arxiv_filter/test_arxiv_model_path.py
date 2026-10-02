# 验证 arXiv 模型路径解析和允许的文件边界。
from __future__ import annotations

from pathlib import Path

from plugins.arxiv_filter.inference import shared
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_explicit_model_path_is_authoritative_over_environment_and_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "configured").mkdir()
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "configured"}},
    )
    monkeypatch.setenv("ARXIV_MODEL_PATH", str(tmp_path / "environment"))

    assert shared.resolve_model_path("explicit") == str(plugin_dir / "explicit")


def test_environment_model_path_is_authoritative_and_does_not_silently_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    configured = plugin_dir / "configured"
    configured.mkdir(parents=True)
    missing_environment = tmp_path / "missing-external-model"
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "configured"}},
    )
    monkeypatch.setenv("ARXIV_MODEL_PATH", str(missing_environment))

    assert shared.resolve_model_path() == str(missing_environment)


def test_config_can_use_legacy_fallback_only_without_an_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    fallback   = plugin_dir / "best_model"
    fallback.mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(fallback)


def test_missing_explicit_configured_model_path_is_not_silently_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "best_model").mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "missing-configured"}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(plugin_dir / "missing-configured")


def test_obsolete_abstract_model_directory_is_not_auto_discovered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    (plugin_dir / "best_model_abs").mkdir(parents=True)
    monkeypatch.setattr(shared, "_PLUGIN_DIR", str(plugin_dir))
    monkeypatch.setattr(
        shared,
        "load_plugin_config",
        lambda: {"model": {"path": "configured"}},
    )
    monkeypatch.delenv("ARXIV_MODEL_PATH", raising=False)

    assert shared.resolve_model_path() == str(plugin_dir / "configured")
